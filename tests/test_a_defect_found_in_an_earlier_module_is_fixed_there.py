"""§17.1831 — a step whose tests keep failing inside another finished module gets that module fixed first.

Live (follow-up 5b4880c0, 2026-10-26): f00ea97f:T4's `write_risk_check` binds its `metrics: dict` straight to a JSONB
column. T6, T6D and T7 -- whose tests ran through it -- failed 11 attempts at `paper_audit_trail_writer.py:131`, a
file none of them may rewrite, each told to `json.dumps` its own values.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import node_editor
from app.modules.execution_retry import retry_failed_node
from app.sandbox import engine_tests as et

text = node_editor.text

# the live failure (folded, §17.1826): the batch's test, the batch, then the writer where the bind fails
LIVE = (
    "§17.1765 engine-test sandbox: its tests failed\n.F.\n"
    "=================================== FAILURES ===================================\n"
    "_________________ test_real_db_writes_proposal_and_risk_check __________________\n"
    "  … 5 library frames …\n"
    "E   TypeError: descriptor 'encode' for 'str' objects doesn't apply to a 'dict' object\n"
    "E   asyncpg.exceptions.DataError: invalid input for query argument $5: {'quantity': '1'} (descriptor 'encode' "
    "for 'str' objects doesn't apply to a 'dict' object)\n"
    "tests/test_paper_daily_signal_proposal_batch.py:484: in test_real_db_writes_proposal_and_risk_check\n"
    "    result = await run_daily_signal_proposal_batch(db=db)\n"
    "app/modules/paper_daily_signal_proposal_batch.py:312: in run_daily_signal_proposal_batch\n"
    "    await write_risk_check(\n"
    "app/modules/paper_audit_trail_writer.py:131: in write_risk_check\n"
    "    async with async_session() as db:\n"
    "  … 16 library frames …\n"
    "____________________ test_batch_counts_orders _____________________\n"
    "tests/test_paper_daily_signal_proposal_batch.py:520: in test_batch_counts_orders\n"
    "    assert result['orders'] == 1\nE   assert 0 == 1\n"
    "_________________ test_own_insert_binds_a_dict _________________\n"
    "app/modules/paper_daily_signal_proposal_batch.py:90: in _insert_item\n"
    "E   asyncpg.exceptions.DataError: invalid input for query argument $4: {'ticker': 'SPY'}\n"
    "=========================== short test summary info ============================\n"
    "3 failed, 1 passed in 2.9s")


def test_a_fault_raised_inside_a_module_is_that_modules_at_its_deepest_frame():
    found = et.callee_defects(LIVE)
    assert [(d["module"], d["frame"], d["test"]) for d in found] == [
        ("paper_audit_trail_writer", "app/modules/paper_audit_trail_writer.py:131 in write_risk_check",
         "test_real_db_writes_proposal_and_risk_check"),
        ("paper_daily_signal_proposal_batch", "app/modules/paper_daily_signal_proposal_batch.py:90 in _insert_item",
         "test_own_insert_binds_a_dict")]                                 # an ordinary assert names no module
    assert "json.dumps(value)" in found[0]["cause"]
    mock = ("_____ test_x _____\napp/modules/paper_x.py:9: in f\n    await db.execute(\n"
            "E   TypeError: 'MagicMock' object can't be awaited\n")
    assert et.callee_defects(mock) == []                                   # the test's fault, not the module's


WRITER = ("```python\n# app/modules/paper_audit_trail_writer.py\nasync def write_risk_check(*, proposal_id, metrics):\n"
          "    ...\n```")
BATCH = "Python module `paper_daily_signal_proposal_batch.py`. The batch."
BRIEF = {"title": "Paper-trading lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}


async def _job(db, reasons: list[str]) -> str:
    jid = str(uuid.uuid4())
    await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                          "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(BRIEF)})
    for i, (k, st, deps, prompt, out) in enumerate([
            ("T4", "done", [], "Python module `paper_audit_trail_writer.py`.", WRITER),
            ("T6", "failed", ["T4"], BATCH, "```python\n# app/modules/paper_daily_signal_proposal_batch.py\nx = 1\n```"),
            ("T7", "failed", ["T6"], "Python module `t7.py`.", WRITER)]):    # a failed draft owns nothing
        nid = (await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, depends_on, prompt_template, output_text, tool, "
            "execution_order, retry_count, max_retries) VALUES (:j, :k, :k, :s, :d, :p, :o, 'CodeGen', :n, :rc, 3) "
            "RETURNING id"),
            {"j": jid, "k": k, "s": st, "d": deps, "p": prompt, "o": out, "n": i, "rc": 3 if k == "T6" else 0})).scalar()
        if k == "T6":
            for r in reasons:
                await db.execute(text("INSERT INTO execution_logs (job_id, node_id, log_level, message, details) "
                                      "VALUES (:j, :n, 'warning', 'Node T6 -> failed', CAST(:d AS jsonb))"),
                                 {"j": jid, "n": nid, "d": json.dumps({"reason": r})})
    await db.commit()
    return jid


async def _drop(jid: str) -> None:
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM execution_logs WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


@pytest.mark.integration
async def test_an_exhausted_step_failing_inside_an_earlier_module_waits_for_its_fix():
    from app.database import async_session
    async with async_session() as db:
        jid = await _job(db, [LIVE, LIVE, "tests failed: assert 0 == 1"])
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T6", db)
        assert out["status"] == "reset" and out["upstream_fix"] == {
            "node_key": "T6", "fix_steps": ["T6U1"], "modules": ["paper_audit_trail_writer"]}
        async with async_session() as db:
            rows = {r["node_key"]: r for r in (await db.execute(text(
                "SELECT node_key, status, depends_on, prompt_template, retry_count, tool_config FROM dag_nodes "
                "WHERE job_id = :j"), {"j": jid})).mappings().all()}
        fix, t6 = rows["T6U1"], rows["T6"]
        assert fix["status"] == "pending" and fix["tool_config"]["fixes_module"] == "paper_audit_trail_writer"
        assert fix["prompt_template"].startswith("Python module `paper_audit_trail_writer.py` -- step T4's module: "
                                                 "edit it in place")
        assert "`app/modules/paper_audit_trail_writer.py:131 in write_risk_check`" in fix["prompt_template"]
        assert "tests/test_paper_audit_trail_writer.py" in fix["prompt_template"]
        assert "paper_daily_signal_proposal_batch" not in fix["prompt_template"].split("Step T6's tests")[0]
        assert t6["status"] == "pending" and t6["retry_count"] == 0 and list(t6["depends_on"]) == ["T4", "T6U1"]
    finally:
        await _drop(jid)


@pytest.mark.integration
async def test_a_fault_in_the_steps_own_module_adds_nothing():
    from app.database import async_session
    own = LIVE.replace("paper_audit_trail_writer", "paper_daily_signal_proposal_batch")
    async with async_session() as db:
        jid = await _job(db, [own, "tests failed: assert 0 == 1"])
        await db.execute(text("UPDATE dag_nodes SET output_text = :o WHERE job_id = :j AND node_key = 'T4'"),
                         {"j": jid, "o": WRITER + "\n```python\n# app/modules/paper_daily_signal_proposal_batch.py\n"
                                                   "old = 1\n```"})     # an earlier copy of the step's own module
        await db.commit()
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T6", db)
        assert out["status"] == "error" and "exhausted" in out["message"]
    finally:
        await _drop(jid)


# ── §17.1832 — a fixed defect is not fixed again; the fix step sees the file it edits ────────────────────────────


async def _log(db, jid: str, key: str, message: str, reason: str, at: int) -> None:
    nid = (await db.execute(text("SELECT id FROM dag_nodes WHERE job_id = :j AND node_key = :k"),
                            {"j": jid, "k": key})).scalar()
    await db.execute(text("INSERT INTO execution_logs (job_id, node_id, log_level, message, details, created_at) "
                          "VALUES (:j, :n, 'warning', :m, CAST(:d AS jsonb), now() + make_interval(secs => :at))"),
                     {"j": jid, "n": nid, "m": message, "d": json.dumps({"reason": reason}), "at": at})


@pytest.mark.integration
async def test_a_defect_its_fix_step_fixed_is_not_fixed_again():
    """Live (5b4880c0 T6): after T6U1 fixed the bind, each exhaustion re-read the refusals from before it and added
    T6U2, T6U3, T6U4, T6U5 for the same frame. Only a failure inside the module AFTER the fix adds another."""
    from app.database import async_session
    other_test = LIVE.replace("test_real_db_writes_proposal_and_risk_check", "test_valid_long_signal_writes_proposal")
    async with async_session() as db:
        jid = await _job(db, [LIVE, other_test])
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T6", db)
            fix = (await db.execute(text("SELECT prompt_template FROM dag_nodes WHERE job_id = :j AND node_key = "
                                         "'T6U1'"), {"j": jid})).scalar()
        assert out["upstream_fix"]["fix_steps"] == ["T6U1"]
        assert fix.count("paper_audit_trail_writer.py:131 in write_risk_check") == 1      # one line per frame
        assert "`test_real_db_writes_proposal_and_risk_check`, `test_valid_long_signal_writes_proposal`" in fix
        async with async_session() as db:                  # the fix finishes; T6 then fails elsewhere, exhausted
            await db.execute(text("UPDATE dag_nodes SET status = 'done' WHERE job_id = :j AND node_key = 'T6U1'"),
                             {"j": jid})
            await db.execute(text("UPDATE dag_nodes SET status = 'failed', retry_count = 3 WHERE job_id = :j "
                                  "AND node_key = 'T6'"), {"j": jid})
            await _log(db, jid, "T6U1", "Node T6U1 -> done", "fixed", 1)
            await _log(db, jid, "T6", "Node T6 -> failed", "tests failed: assert 0 == 1", 2)
            await db.commit()
        async with async_session() as db:
            again = await retry_failed_node(jid, "T6", db)
        assert "upstream_fix" not in again
        async with async_session() as db:                  # a NEW failure inside the module, after the fix
            await db.execute(text("UPDATE dag_nodes SET status = 'failed', retry_count = 3 WHERE job_id = :j "
                                  "AND node_key = 'T6'"), {"j": jid})
            await _log(db, jid, "T6", "Node T6 -> failed", LIVE.replace(":131", ":140"), 3)
            await db.commit()
        async with async_session() as db:
            third = await retry_failed_node(jid, "T6", db)
            fix2 = (await db.execute(text("SELECT prompt_template FROM dag_nodes WHERE job_id = :j AND node_key = "
                                          "'T6U2'"), {"j": jid})).scalar()
        assert third["upstream_fix"]["fix_steps"] == ["T6U2"]
        assert ":140 in write_risk_check" in fix2 and ":131" not in fix2
    finally:
        await _drop(jid)


def test_a_step_that_edits_a_module_sees_it_as_it_is_now():
    """Live (5b4880c0): T6U2-T6U4 were told to edit the writer in place and shown only that T6U1 had rewritten it;
    each wrote a new one, the last with every writer taking one record object."""
    from app.modules import engine_source as es
    v1 = WRITER.replace("metrics):", "metrics, data_ts=None):")
    rows = [{"node_key": "f00ea97f:T4", "status": "done", "title": "w",
             "prompt_template": "Python module `paper_audit_trail_writer.py`.", "output_text": WRITER},
            {"node_key": "T6U1", "status": "done", "title": "Fix", "prompt_template": "", "output_text": v1},
            {"node_key": "T6U2", "status": "failed", "title": "Fix", "prompt_template": "",       # a failed draft
             "output_text": WRITER.replace("metrics):", "check):")}]
    block = es.edited_module_block(rows, "paper_audit_trail_writer")
    assert "(step T6U1)" in block and "metrics, data_ts=None" in block and "check)" not in block
    assert "keep every public function, class and parameter" in block
    assert es.edited_module_block(rows, "paper_nothing") == ""
    line = es.carried_block(rows[:1], {"title": "batch"}, rewritten=es.rewritten_here(rows),
                            current=es.current_sources(rows))
    assert "REWRITTEN by this job's step T6U1: use that step's version, not this one; it now has:\n" \
           "        write_risk_check(*, proposal_id, metrics, data_ts)" in line


@pytest.mark.integration
async def test_this_jobs_own_steps_come_back_in_plan_order():
    """Live: the name check keeps the last writer of a module; this job's rows came back in heap order and it cited
    T6U4's writer after T6U5 had finished."""
    from app.database import async_session
    from app.modules import engine_source as es
    async with async_session() as db:
        jid = await _job(db, [])
        for k, n in (("T6U5", 23), ("T6U4", 22)):                       # inserted out of plan order
            await db.execute(text("INSERT INTO dag_nodes (job_id, node_key, title, status, tool, execution_order) "
                                  "VALUES (:j, :k, :k, 'done', 'CodeGen', :n)"), {"j": jid, "k": k, "n": n})
        await db.execute(text("UPDATE dag_nodes SET execution_order = 30 WHERE job_id = :j AND node_key = 'T4'"),
                         {"j": jid})
        await db.commit()
    try:
        async with async_session() as db:
            keys = [r["node_key"] for r in await es.code_rows(db, jid)]
        assert keys == ["T6", "T7", "T6U4", "T6U5", "T4"]
    finally:
        await _drop(jid)
