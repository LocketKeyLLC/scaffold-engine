"""§17.1856 — a patch target names what the module has, or the attempt is refused before the sandbox.

Live (follow-up 5b4880c0 T5 "Write tests for twelve modules", 2026-10-28, the §17.1855 round, attempt 3): the test
patched `app.modules.paper_operator_approval_gate.write_approval`. The gate writes `approval` with its own SQL and
never imports `write_approval` (`paper_audit_trail_writer` defines it), so the sandbox raised "AttributeError: module
… does not have the attribute 'write_approval'", one of seven failures that spent the attempt. The engine held
every fact -- the module's names, its imports, where the name lives, the table it writes -- and said nothing.
"""
from __future__ import annotations

import pytest

from app.modules.engine_contracts import _top_names, unknown_patch_targets

GATE = '''import logging
from typing import Any, Optional

from sqlalchemy import text

from app.database import async_session
from app.modules.operator_ask import answer_handler, decision, open_decision_job

APPROVAL_MODULE = "app.modules.paper_operator_approval_gate"


async def _record_approval(db, proposal, operator_id):
    await db.execute(text("INSERT INTO approval (proposal_id, operator_id, decision) VALUES (:p, :o, 'approved')"),
                     {"p": proposal["id"], "o": operator_id})


async def request_approval(proposal: dict[str, Any], *, db: Optional[Any] = None) -> dict[str, Any]:
    return {"job_id": "j", "proposal": proposal}


async def handle_approval_answer(job_id, node_key, answer, payload):
    return {"status": "approved"}
'''
WRITER = '''from sqlalchemy import text

async def write_approval(*, db, proposal_id, operator_id, decision, reason):
    return (await db.execute(text("INSERT INTO approval (proposal_id, operator_id, decision, reason) VALUES "
                                  "(:p, :o, :d, :r) RETURNING id"), {})).scalar_one()
'''
MODULES = {"paper_operator_approval_gate": GATE, "paper_audit_trail_writer": WRITER}
BATCH = '''# app/modules/paper_tests_runner.py
from app.modules.paper_operator_approval_gate import handle_approval_answer
'''


def _files(test: str) -> dict[str, str]:
    return {"app/modules/paper_tests_runner.py": BATCH, "tests/test_paper_twelve_modules.py": test}


def test_a_modules_top_level_names_are_what_it_defines_and_what_it_imports():
    assert _top_names(GATE) == (["APPROVAL_MODULE", "_record_approval", "request_approval", "handle_approval_answer"],
                                ["logging", "Any", "Optional", "text", "async_session", "answer_handler", "decision",
                                 "open_decision_job"])
    assert _top_names("def f(:\n") == ([], [])
    assert _top_names("from __future__ import annotations\nimport app.x as ax\nX: int = 1\n") == (["X"], ["ax"])


def test_the_live_patch_of_a_name_the_gate_never_had_is_refused_with_where_it_lives():
    test = '''from unittest.mock import AsyncMock, patch

async def test_x():
    with patch("app.modules.paper_operator_approval_gate.write_approval", new=AsyncMock()):
        pass
'''
    found = unknown_patch_targets(_files(test), MODULES, {"paper_tests_runner"})
    assert found == [
        "`tests/test_paper_twelve_modules.py:4` patches `write_approval` on `paper_operator_approval_gate`, which has no "
        "such name (it has APPROVAL_MODULE, request_approval, handle_approval_answer; it imports logging, Any, Optional, "
        "text, async_session, answer_handler, decision, open_decision_job) -- the sandbox raises AttributeError. "
        "`write_approval` is defined in `paper_audit_trail_writer`, which `paper_operator_approval_gate` never imports: "
        "patching it anywhere changes nothing `paper_operator_approval_gate` does -- `paper_operator_approval_gate` "
        "writes `approval` itself: call it and read the rows it wrote. Patch a name where the code looks it up, and "
        "only what reaches outside the engine"]


def test_every_way_of_naming_the_target_and_every_name_the_module_has_are_read():
    test = '''from unittest.mock import patch
from app.modules import paper_operator_approval_gate as gate
import app.modules.paper_operator_approval_gate as g2

def test_a(monkeypatch):
    with patch.object(gate, "write_approval"):                                 # the object form
        pass
    monkeypatch.setattr(g2, "nothing_here", None)                              # the import-as form
    monkeypatch.setattr("app.modules.paper_operator_approval_gate.request_approval", None)   # defined: fine
    with patch("app.modules.paper_operator_approval_gate.text"):               # imported: fine
        pass
    with patch("app.modules.paper_operator_approval_gate._record_approval"):   # private but defined: fine
        pass
    with patch("app.modules.paper_operator_approval_gate.APPROVAL_MODULE", "x"):   # a constant: fine
        pass
    with patch("app.modules.paper_tests_runner.handle_approval_answer"):       # the step's own module: fine
        pass
    with patch("app.modules.not_this_job.anything"):                           # not a job module: not ours
        pass
    with patch("app.modules.paper_operator_approval_gate.write_approval"):     # the same miss twice: once
        pass
'''
    found = unknown_patch_targets(_files(test), MODULES, {"paper_tests_runner"})
    assert [f.split(" -- ")[0] for f in found] == [
        "`tests/test_paper_twelve_modules.py:6` patches `write_approval` on `paper_operator_approval_gate`, which has no "
        "such name (it has APPROVAL_MODULE, request_approval, handle_approval_answer; it imports logging, Any, Optional, "
        "text, async_session, answer_handler, decision, open_decision_job)",
        "`tests/test_paper_twelve_modules.py:8` patches `nothing_here` on `paper_operator_approval_gate`, which has no "
        "such name (it has APPROVAL_MODULE, request_approval, handle_approval_answer; it imports logging, Any, Optional, "
        "text, async_session, answer_handler, decision, open_decision_job)"]
    assert "is defined in" not in found[1]                                      # nowhere in the job: no claim
    assert unknown_patch_targets({"tests/test_x.py": "x = 1\n"}, MODULES, set()) == []


@pytest.mark.integration
async def test_the_engines_findings_refuse_the_patch():
    """The wiring: `engine_findings` runs it with the job's finished modules."""
    import json
    import uuid

    from app.database import async_session
    from app.modules import engine_source, node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    brief = {"title": "lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}
    test = ('```python\n# tests/test_paper_twelve_modules.py\nfrom unittest.mock import patch\n\n'
            'def test_x():\n    with patch("app.modules.paper_operator_approval_gate.write_approval"):\n        pass\n```')
    output = f"```python\n{BATCH}```\n\n{test}"
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        for i, (k, st, prompt, out) in enumerate([
                ("T16", "done", "Python module `paper_operator_approval_gate.py`.",
                 f"```python\n# app/modules/paper_operator_approval_gate.py\n{GATE}```"),
                ("T4", "done", "Python module `paper_audit_trail_writer.py`.",
                 f"```python\n# app/modules/paper_audit_trail_writer.py\n{WRITER}```"),
                ("T5", "running", "Python module `paper_tests_runner.py`.", None)]):
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
                "execution_order) VALUES (:j, :k, :k, :s, :p, :o, 'CodeGen', :n)"),
                {"j": jid, "k": k, "s": st, "p": prompt, "o": out, "n": i})
        await db.commit()
    try:
        async with async_session() as db:
            found = await engine_source.engine_findings(db, jid, "T5", output)
        assert any(f.startswith("`tests/test_paper_twelve_modules.py:5` patches `write_approval` on "
                                "`paper_operator_approval_gate`, which has no such name") for f in found), found
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
