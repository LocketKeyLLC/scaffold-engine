"""§17.1991 — a finding at a line the step only KEPT from the engine's own copy of the module it edits is that copy's
defect, not the step's: it is not refused, the step is told to keep the line, and the operator is told against the job
that owns the line.

Live (69fe61c4 T8 "Add dashboard route", 2026-11-21): T8 extends the lab's merged router in place. 11 of its 16 §17.1749
refusals carried "`app/modules/paper_wire_engine_entry_module.py:<n>` calls `run_execution_entry_point(…)` is missing
required argument(s) `db`, `operator_id`, `timestamp`" -- the POST /execution route job 5b4880c0 (step T9) delivered in
#1161, which T8 kept as it was and could not fix (which operator id?). The same refusals' `get_spy_benchmark(…)` call
and an unused `spy` were T8's own lines, and stayed refusals.

The fixtures are made up: an engine module `lab_wire` (in a temporary engine tree) whose route calls a real engine
function with no arguments, the way the lab's router did."""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import engine_source as es

PATH = "app/modules/lab_wire.py"
#: the engine's copy, as merged: its /execution route calls a real engine coroutine with none of its arguments
BASE = '''"""Wire the lab's routes into the engine."""
from typing import Any

from fastapi import APIRouter

from app.modules.engine_source import carried_modules, code_rows

router = APIRouter()


@router.post("/execution")
async def execution() -> Any:
    result = await code_rows()
    return result
'''
#: the step's edit: the route kept as it was, one new route of its own -- with a bad call of its own
NEW_ROUTE = '''

@router.get("/dashboard")
async def dashboard(span: str = "1m") -> Any:
    mods = await carried_modules()
    return {"span": span, "modules": mods}
'''
INHERITED = ("calls `code_rows(…)` is missing required argument(s) `db`, `job_id` -- the signature is "
             "`async code_rows(db, job_id: str)`")


def _out(code: str) -> str:
    return f"The route, added in place.\n\n```python\n# {PATH}\n{code}```\n"


def _line_of(code: str, text: str) -> int:
    return next(i for i, x in enumerate(f"# {PATH}\n{code}".splitlines(), 1) if x.strip() == text)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    es.module_index()
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "lab_wire.py").write_text(BASE)
    monkeypatch.setattr(es, "ENGINE_PKG", tmp_path)
    return tmp_path


# ── the pure parts, on the live finding's shape ─────────────────────────────────────────────────────────────────

def test_a_finding_on_a_kept_line_that_the_engines_copy_also_has_is_inherited(tree):
    code = BASE + NEW_ROUTE
    kept, own = _line_of(code, "result = await code_rows()"), _line_of(code, "mods = await carried_modules()")
    found = [f"`{PATH}:{kept}` {INHERITED}",
             f"`{PATH}:{own}` calls `carried_modules(…)` is missing required argument(s) `db`, `job_id`",
             "`from app.modules.lab_dashboard_context import …`: the engine has no module `app.modules.lab_dashboard_context`"]
    cands = es.inherited_candidates(found, _out(code), [])
    assert list(cands) == [PATH] and [i for i, *_ in cands[PATH]["items"]] == [0]     # the step's own line is no candidate
    base_line = _line_of(BASE, "result = await code_rows()")
    assert es.inherited_of(PATH, cands[PATH], [f"`{PATH}:{base_line}` {INHERITED}"]) == {0}
    # the engine's copy does not make that finding: the step's edit made it (a miss)
    assert es.inherited_of(PATH, cands[PATH], [f"`{PATH}:{base_line}` calls `code_rows(…)` takes at most 2"]) == set()


def test_a_bad_line_the_step_writes_again_in_its_own_code_is_its_own(tree):
    code = BASE + NEW_ROUTE.replace("mods = await carried_modules()", "result = await code_rows()")
    lines = [i for i, x in enumerate(f"# {PATH}\n{code}".splitlines(), 1) if x.strip() == "result = await code_rows()"]
    found = [f"`{PATH}:{n}` {INHERITED}" for n in lines]
    assert len(lines) == 2 and es.inherited_candidates(found, _out(code), []) == {}


def test_a_module_the_job_holds_undelivered_is_the_jobs_own():
    rows = [{"node_key": "T3", "status": "done", "output_text": _out(BASE)}]
    found = [f"`{PATH}:{_line_of(BASE, 'result = await code_rows()')}` {INHERITED}"]
    assert es.engine_copy(rows, PATH) is None                      # not in a temporary tree, and the job's anyway
    assert es.inherited_candidates(found, _out(BASE), rows) == {}


def test_a_delivered_module_not_rewritten_since_is_the_engines_reviewed_copy(tree):
    from datetime import datetime, timezone
    t0 = datetime(2026, 11, 7, tzinfo=timezone.utc)
    rows = [{"node_key": "5b4880c0:T9", "status": "done", "output_text": _out(BASE), "completed_at": t0},
            {"node_key": "5b4880c0:DELIVER", "status": "done", "output_text": "",
             "title": "Is this job's code merged into the engine and running?", "completed_at": t0}]
    assert es.engine_copy(rows, PATH) == (BASE, "5b4880c0:T9")
    assert es.engine_copy(rows[:1], PATH) is None                  # never delivered: still that job's
    found = [f"`{PATH}:{_line_of(BASE, 'result = await code_rows()')}` {INHERITED}"]
    assert list(es.inherited_candidates(found, _out(BASE), rows)) == [PATH]          # a job this one builds on
    own = [{**r, "node_key": r["node_key"].split(":")[1]} for r in rows]
    assert es.engine_copy(own, PATH) == (BASE, "T9")
    assert es.inherited_candidates(found, _out(BASE), own) == {}   # delivered by this job: its defects are its own


@pytest.mark.parametrize("me", [{"title": "Fix lab_wire (the /execution route's call)"},
                                {"title": "Repair route", "prompt_template": "Fix `lab_wire.py`: pass db, job"}])
def test_a_step_that_fixes_the_module_is_refused_for_a_line_it_kept(tree, me):
    """Live (5b4880c0 FEX1, 2026-11-22): the operator's fix of this route edits the same file; a draft that keeps the
    line must stay refused, or the fix passes without fixing."""
    found = [f"`{PATH}:{_line_of(BASE, 'result = await code_rows()')}` {INHERITED}"]
    assert es.inherited_candidates(found, _out(BASE), [], me) == {}
    assert list(es.inherited_candidates(found, _out(BASE), [], {"title": "Add dashboard route"})) == [PATH]


def test_the_step_is_told_the_line_is_not_its_own_and_its_refusal_kinds_do_not_count_it():
    t = {"path": PATH, "line_no": 80, "line": "result = await code_rows()", "finding": INHERITED, "step": "T8",
         "job": "5b4880c0", "owner_step": "T9", "changed_by": []}
    reason = es.unresolved_reason(["`tests/test_lab_wire.py:12` uses `FastAPI`, which the file never imports"], [t])
    assert "Not yours (§17.1991)" in reason and "owned by job 5b4880c0 step T9" in reason
    assert "line 80: `result = await code_rows()`" in reason and "Keep each exactly as it is" in reason
    assert "missing required argument" not in reason                 # the refusal kinds read the reason's text
    assert "\n  - `app/modules/lab_wire.py" not in reason              # not a finding bullet
    assert es.unresolved_reason(["x"]) == es.unresolved_reason(["x"], [])


# ── the gate itself, on a real database ────────────────────────────────────────────────────────────────────────

async def _job(db, jid: str, nodes: list[tuple[str, str, str]]) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text
    t0 = datetime(2026, 11, 4, tzinfo=timezone.utc)
    brief = {"title": "dash", "description": "Deliver the dashboard into the engine through the lab's delivery flow."}
    await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                          "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
    for i, (key, status, out) in enumerate(nodes):
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, tool, execution_order, "
            "output_text, completed_at) VALUES (:j, :k, :k, :s, 'Python module `lab_wire.py`', 'CodeGen', :o, :t, :c)"),
            {"j": jid, "k": key, "s": status, "o": i, "t": out,
             "c": t0 + timedelta(days=i) if status == "done" else None})


@pytest.mark.integration
@pytest.mark.timeout(300)
async def test_engine_findings_keeps_the_inherited_call_out_of_the_refusal_and_tells_the_operator(tree):
    from sqlalchemy import text

    from app.database import async_session
    owner, fixed, jid = str(uuid.uuid4()), "FEX1", str(uuid.uuid4())
    code = BASE + NEW_ROUTE
    async with async_session() as db:
        # the owning job: T9 wrote the route, a later FEX1 changed it (not delivered yet)
        await _job(db, owner, [("T9", "done", _out(BASE)),
                               (fixed, "done", _out(BASE.replace("await code_rows()", "await code_rows(db, job)")))])
        await _job(db, jid, [("T8", "running", None)])
        await db.commit()
    try:
        inherited: list[dict] = []
        async with async_session() as db:
            found = await es.engine_findings(db, jid, "T8", _out(code), inherited)
        assert not any("code_rows(…)" in f for f in found), found                 # not refused
        assert any("carried_modules(…)` is missing required argument" in f for f in found), found   # its own: refused
        assert [(t["path"], t["line"], t["job"], t["owner_step"], t["changed_by"]) for t in inherited] == [
            (PATH, "result = await code_rows()", owner[:8], "T9", [fixed])]
        assert "is missing required argument(s) `db`, `job_id`" in inherited[0]["finding"]
        assert await es.surface_inherited(jid, inherited) == 1
        assert await es.surface_inherited(jid, inherited) == 0                    # one alert per line and finding
        async with async_session() as db:
            msg = (await db.execute(text(
                "SELECT message FROM system_alerts WHERE kind = 'engine_code.inherited_defect' AND payload->>'job_id' = :j"),
                {"j": jid})).scalars().all()
        assert len(msg) == 1 and f"owned by job {owner[:8]} step T9" in msg[0] and "changed since by FEX1" in msg[0]
        # the gate still bites on the line once the step writes it again in its own route
        twice = BASE + NEW_ROUTE.replace("mods = await carried_modules()", "result = await code_rows()")
        async with async_session() as db:
            again = await es.engine_findings(db, jid, "T8", _out(twice), [])
        assert sum("code_rows(…)` is missing required argument" in f for f in again) == 2, again
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM system_alerts WHERE kind = 'engine_code.inherited_defect' "
                                  "AND payload->>'job_id' = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id IN (:a, :b)"), {"a": jid, "b": owner})
            await db.commit()
