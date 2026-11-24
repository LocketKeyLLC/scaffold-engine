"""§17.2008 — a module the step edits in place is checked against what the step writes, and a module bound by
`pytest.importorskip(…)` is a patch target like an imported one.

Live (69fe61c4 NOKEY2, 2026-11-24): four attempts were refused. Each attempt's tests called the new
`entry._load_spy(…)` / `entry._spy_missing(…)` the same answer added to `paper_wire_engine_entry_module`, and were told
the module "(step NOKEY) has no `_load_spy`" -- NOKEY's file, the one being replaced (§17.1816 set aside only a row
carried from an earlier job). And the no-key fixture was `alpaca = pytest.importorskip("app.modules.…client")` then
`monkeypatch.setattr(alpaca, "values_for", _empty)`: the view gate read no patch and refused for a test it had."""
from __future__ import annotations

import ast
import json
import uuid

import pytest

from app.modules import engine_contracts as ec
from app.modules import engine_source as es
from tests.test_a_view_answers_without_a_secret_not_stored_17_2003 import ENGINE, HEAD, WIRE, WIRE_PATH, TEST_PATH

# ── a module bound by a call ──────────────────────────────────────────────────────────────────────────────────────

FIXTURE_NO_KEY = HEAD + '''
import pytest


@pytest.fixture()
def no_lab_keys(monkeypatch):
    client = pytest.importorskip("app.modules.lab_client")

    async def _empty(db, wanted=None):
        return {}

    monkeypatch.setattr(client, "values_for", _empty, raising=False)
    return client


def test_dashboard_answers_without_the_lab_keys(no_lab_keys):
    assert _client().get("/labs/lab/dashboard?span=all").status_code == 200
'''


def test_a_module_bound_by_importorskip_is_a_patch_target_and_its_stand_in_is_empty():
    tree = ast.parse(FIXTURE_NO_KEY)
    assert ("lab_client", "values_for") in {(m, n) for m, n, _ln in ec._patch_targets(FIXTURE_NO_KEY)}
    assert [m for m, n, _ln in ec._empty_secret_patches(tree, FIXTURE_NO_KEY)] == ["lab_client"]


def test_the_view_gate_reads_the_live_fixture_shape():
    files = {WIRE_PATH: WIRE, TEST_PATH: FIXTURE_NO_KEY}
    assert ec.views_fail_without_secrets(files, ENGINE, {"lab_wire"}) == []


def test_a_bound_name_that_is_not_a_job_module_is_no_target():
    # the correct miss: a name bound to anything else is not read as a module
    code = FIXTURE_NO_KEY.replace('pytest.importorskip("app.modules.lab_client")', 'pytest.importorskip("httpx")')
    assert ec._patch_targets(code) == []
    assert ec.views_fail_without_secrets({WIRE_PATH: WIRE, TEST_PATH: code}, ENGINE, {"lab_wire"})


def test_importlib_and_a_file_level_binding_count_too():
    code = '''import importlib

client = importlib.import_module("app.modules.lab_client")


def test_x(monkeypatch):
    monkeypatch.setattr(client, "values_for", lambda *a: {})
'''
    assert [(m, n) for m, n, _ln in ec._patch_targets(code)] == [("lab_client", "values_for")]


# ── a module the step rewrites ────────────────────────────────────────────────────────────────────────────────────

MOD = "lab_entry"
OLD = f'''# app/modules/{MOD}.py
def load():
    return 1
'''
NEW = f'''# app/modules/{MOD}.py
def load():
    return _load_more()


def _load_more():
    return 2
'''
TESTS = f'''# tests/test_{MOD}.py
from app.modules import {MOD} as entry


def test_load():
    assert entry.load() == 2


def test_load_more():
    assert entry._load_more() == 2
'''


def _out(*blocks: str) -> str:
    return "\n\n".join(f"```python\n{b}```" for b in blocks)


@pytest.mark.integration
async def test_a_module_this_job_wrote_and_the_step_edits_in_place_is_checked_against_the_new_file():
    from app.database import async_session
    from app.modules import node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    brief = {"title": "lab", "description": "Add a research lab to the scaffold engine's own codebase."}
    fix = f"Edit `{MOD}.py` in place so that load() reads more."
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        for i, (k, title, st, prompt, out, cfg) in enumerate([
                ("T1", "Write the entry module", "done", f"Python module `{MOD}.py`.", _out(OLD), None),
                ("FIX1", f"Fix {MOD}", "running", fix, None, json.dumps({"fixes_module": MOD}))]):
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
                "execution_order, tool_config) VALUES (:j, :k, :t, :s, :p, :o, 'CodeGen', :n, CAST(:c AS jsonb))"),
                {"j": jid, "k": k, "t": title, "s": st, "p": prompt, "o": out, "n": i, "c": cfg})
        await db.commit()
    try:
        async with async_session() as db:
            found = await es.engine_findings(db, jid, "FIX1", _out(NEW, TESTS))
        assert not [f for f in found if "has no `_load_more`" in f], found
        # the correct miss: a name the new file does not define either is still refused
        async with async_session() as db:
            found = await es.engine_findings(db, jid, "FIX1", _out(NEW, TESTS.replace("entry._load_more()",
                                                                                  "entry._load_most()")))
        assert any("has no `_load_most`" in f for f in found), found
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
