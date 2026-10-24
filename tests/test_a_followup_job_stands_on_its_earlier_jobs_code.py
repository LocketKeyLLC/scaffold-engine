"""§17.1811 — a follow-up job's code is written against the modules its earlier job finished.

Live (lab f00ea97f -> its follow-up, 2026-10-24): 23 trading modules exist only as the finished steps of f00ea97f.
Every reader of "this job's code" -- the engine block in the prompt (§17.1749/1796), the name checks
(`engine_findings`), the module list (`job_code`) and the engine-test sandbox (§17.1765) -- read `WHERE job_id = :j`,
so a new job fixing the order executor would see none of them: it would rewrite them or guess at their names.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.modules import engine_source as es
from app.sandbox import engine_tests as et

PRIOR = "f00ea97f-0000-4000-8000-000000000000"
CLIENT = {"node_key": "T5", "title": "Implement Alpaca paper client", "description": "", "status": "done",
          "prompt_template": "Python module `paper_alpaca_paper_client.py`",
          "output_text": "```python\n# app/modules/paper_alpaca_paper_client.py\nasync def get_bars(symbol):\n    ...\n```"}
EXECUTOR = {"node_key": "T17", "title": "Implement order executor", "description": "", "status": "done",
            "prompt_template": "Python module `paper_order_executor.py`",
            "output_text": "```python\n# app/modules/paper_order_executor.py\nOLD = 'ticker'\n```"}


def _db(rows):
    """A session answering `code_rows`' one read with rows as the query returns them."""
    r = MagicMock()
    r.mappings.return_value.all.return_value = [{"src": 0, "ord": 1, **x} for x in rows]
    db = AsyncMock()
    db.execute = AsyncMock(return_value=r)
    return db


CARRIED = [{**CLIENT, "node_key": "f00ea97f:T5"}, {**EXECUTOR, "node_key": "f00ea97f:T17"}]


@pytest.mark.integration
async def test_the_earlier_jobs_finished_steps_come_first_under_their_own_keys():
    from sqlalchemy import text

    from app.database import async_session
    prior, other, job = (str(uuid.uuid4()) for _ in range(3))
    ins = ("INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
           "execution_order) VALUES (:j, :k, :t, :s, '', :o, 'CodeGen', :n)")
    try:
        async with async_session() as db:
            for jid, meta in ((prior, {}), (other, {}), (job, {"builds_on": [prior]})):
                await db.execute(text("INSERT INTO jobs (id, title, status, metadata) VALUES (:id, 'x', 'completed', "
                                      "CAST(:m AS jsonb))"), {"id": jid, "m": json.dumps(meta)})
            for k, s_, o, n in (("T17", "done", "executor", 2), ("T5", "done", "client", 1),
                                ("T30", "skipped", "never", 3), ("T31", "done", None, 4)):
                await db.execute(text(ins), {"j": prior, "k": k, "t": k, "s": s_, "o": o, "n": n})
            await db.execute(text(ins), {"j": other, "k": "T9", "t": "T9", "s": "done", "o": "other job", "n": 1})
            await db.execute(text(ins), {"j": job, "k": "T1", "t": "T1", "s": "pending", "o": None, "n": 1})
            await db.commit()
        async with async_session() as db:
            rows = await es.code_rows(db, job)
            alone = await es.code_rows(db, prior)
        p8 = prior[:8]
        assert [r["node_key"] for r in rows] == [f"{p8}:T5", f"{p8}:T17", "T1"]     # done, written, in plan order
        assert rows[0]["output_text"] == "client" and set(rows[0]) == {
            "node_key", "title", "description", "prompt_template", "output_text", "status"}
        assert sorted(r["node_key"] for r in alone) == ["T17", "T30", "T31", "T5"]   # no link: only itself, as before
    finally:
        async with async_session() as db:
            for jid in (prior, other, job):
                await db.execute(text("DELETE FROM dag_nodes WHERE job_id = :j"), {"j": jid})
                await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


@pytest.mark.asyncio
async def test_the_module_list_and_the_name_checks_see_the_earlier_jobs_modules():
    own = {"node_key": "T1", "title": "Fix the order executor", "description": "", "status": "pending",
           "prompt_template": "Python module `paper_order_executor.py`", "output_text": None}
    mods, names = await es.job_code(_db([CARRIED[0], own]), "job-2", except_node="T1")
    assert "paper_alpaca_paper_client" in mods and "get_bars" in names


@pytest.mark.asyncio
async def test_in_the_sandbox_this_jobs_file_replaces_the_earlier_jobs_copy():
    rows = await es.code_rows(_db(CARRIED), "job-2")
    me = {"node_key": "T1", "prompt_template": "Python module `paper_order_executor.py`"}
    mine = "```python\n# app/modules/paper_order_executor.py\nNEW = 'symbol'\n```"
    run = AsyncMock(return_value=SimpleNamespace(ok=True, error=None, database="ok", exit_code=0, stdout="", stderr="",
                                                 timed_out=False))
    with patch.object(et.settings, "engine_test_check_enabled", True), \
         patch.object(et.settings, "engine_test_url", "http://sandbox:8020"), \
         patch.object(et, "_sandbox_engine", new=AsyncMock(return_value="d")), \
         patch.object(et, "_local_digest", return_value="d"), \
         patch.object(et, "run_code", new=run):
        res = await et.check(mine, me, rows)
    files = run.await_args.args[0]
    assert res.verdict == "pass"
    assert "get_bars" in files["app/modules/paper_alpaca_paper_client.py"]        # the earlier job's module is there
    assert "NEW = 'symbol'" in files["app/modules/paper_order_executor.py"]       # and this job's fix replaces its own


# ── the production path ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_executor_hands_the_sandbox_the_earlier_jobs_code():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    carried = [{**CLIENT, "node_key": "f00ea97f:T5"}]
    spy = AsyncMock(return_value=SimpleNamespace(verdict="pass", reason="ok", ran=[]))
    with patch("app.sandbox.engine_tests.check", new=spy), \
         patch("app.modules.engine_source.code_rows", new=AsyncMock(return_value=carried)), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    spy.assert_awaited()
    assert spy.await_args.args[2] == carried, captured.get("raised")


@pytest.mark.asyncio
async def test_the_executor_shows_the_prompt_the_earlier_jobs_modules():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run
    carried = [{**CLIENT, "node_key": "f00ea97f:T5"}]
    with patch("app.sandbox.engine_tests.check", new=AsyncMock(return_value=SimpleNamespace(
                verdict="pass", reason="ok", ran=[]))), \
         patch("app.modules.engine_source.code_rows", new=AsyncMock(return_value=carried)), \
         patch("app.modules.engine_source.job_code", new=AsyncMock(return_value=(set(), set()))), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
    assert "get_bars" in captured["calls"][0][-1]["content"], captured.get("raised")


# ── the endpoint ─────────────────────────────────────────────────────────────

def _endpoint_db(status):
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=status)))
    db.commit = AsyncMock()
    return db


async def _start(db, builds_on):
    from app.routers import workflow as wf
    from app.schemas import IdeaInput
    body = IdeaInput(idea="Finish the lab", builds_on=builds_on)
    with patch.object(wf, "_require_valid_models", new=AsyncMock()), \
         patch.object(wf, "create_ideation_job", new=AsyncMock(return_value="job-2")), \
         patch.object(wf, "spawn_phase1_background") as spawn:
        out = await wf.ideate_start_endpoint(body, db=db, principal=SimpleNamespace(identity="op"))
    return out, spawn


@pytest.mark.asyncio
async def test_a_job_is_started_on_a_finished_job_and_keeps_the_link():
    db = _endpoint_db("completed")
    out, spawn = await _start(db, [PRIOR])
    assert out["job_id"] == "job-2" and spawn.called
    writes = [c for c in db.execute.await_args_list if "builds_on" in str(c.args[0])]
    assert writes and json.loads(writes[0].args[1]["b"]) == [PRIOR] and writes[0].args[1]["j"] == "job-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "failed", None])
async def test_a_job_cannot_build_on_one_that_did_not_finish(status):
    with pytest.raises(HTTPException) as e:
        await _start(_endpoint_db(status), [str(uuid.uuid4())])
    assert e.value.status_code == 422 and "not completed" in e.value.detail


@pytest.mark.asyncio
async def test_without_builds_on_nothing_extra_is_written():
    db = _endpoint_db("completed")
    await _start(db, [])
    assert not [c for c in db.execute.await_args_list if "builds_on" in str(c.args[0])]


@pytest.mark.asyncio
async def test_the_name_checks_accept_a_call_to_the_earlier_jobs_module_and_refuse_a_guess():
    own = {"node_key": "T1", "title": "Fix the order executor", "description": "", "status": "pending",
           "prompt_template": "Python module `paper_order_executor.py`", "output_text": None}
    nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [{"src": 0, "ord": 1, **CARRIED[0]}, own]
    cols = MagicMock(); cols.all.return_value = []

    async def findings(out):
        db = AsyncMock(); db.execute = AsyncMock(side_effect=[nodes, cols])
        return "\n".join(await es.engine_findings(db, "job-2", "T1", out))
    head = "```python\n# app/modules/paper_order_executor.py\nfrom app.modules.paper_alpaca_paper_client import "
    assert "get_bars" not in await findings(head + "get_bars\n```")
    assert "get_price_history" in await findings(head + "get_price_history\n```")
