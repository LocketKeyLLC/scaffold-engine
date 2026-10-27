"""§17.1848 — a code step that keeps repeating one kind of refusal runs its next round on the other code model.

Live (follow-up 5b4880c0 T6, 2026-10-27): deepseek-v4-pro (pinned on the step by the planner) stood in for a finished
data module in its tests in every attempt of four rounds, through exact refusals and the list of what may be mocked.
Operator choice: switch the model.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.config import get_model
from app.modules import node_editor
from app.modules.execution_retry import retry_failed_node

text = node_editor.text

FAKE = ("§17.1749 this step writes code for the engine itself, and it reaches for things the engine does not have:\n"
        "  - `tests/test_paper_batch.py:{n}` replaces `load_batch_signals` (`paper_batch_data`, a finished module "
        "that never leaves the engine) with a stand-in: call the real one ...")
BRIEF = {"title": "lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}


async def _job(db, reasons: list[str], model: str) -> str:
    jid = str(uuid.uuid4())
    await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                          "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(BRIEF)})
    nid = (await db.execute(text(
        "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, tool, assigned_model, execution_order, "
        "retry_count, max_retries) VALUES (:j, 'T6', 'Batch', 'failed', 'Python module `paper_batch.py`.', 'CodeGen', "
        ":m, 1, 3, 3) RETURNING id"), {"j": jid, "m": model})).scalar()
    for i, r in enumerate(reasons):
        await db.execute(text("INSERT INTO execution_logs (job_id, node_id, log_level, message, details, created_at) "
                              "VALUES (:j, :n, 'warning', 'Node T6 -> failed', CAST(:d AS jsonb), "
                              "now() + make_interval(secs => :i))"),
                         {"j": jid, "n": nid, "d": json.dumps({"reason": r}), "i": i})
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
async def test_a_repeated_refusal_switches_the_model_once_with_fresh_attempts():
    from app.database import async_session
    general, coder = get_model("model_general", None), get_model("model_coder", None)
    assert general != coder
    async with async_session() as db:
        jid = await _job(db, [FAKE.format(n=37), "tests failed: assert 0 == 1", FAKE.format(n=115)], general)
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T6", db)
        assert out["status"] == "reset" and out["model_switch"] == {
            "node_key": "T6", "from": general, "to": coder, "for": ["stand-in"]}
        async with async_session() as db:
            r = (await db.execute(text("SELECT status, retry_count, assigned_model, tool_config FROM dag_nodes "
                                       "WHERE job_id = :j AND node_key = 'T6'"), {"j": jid})).mappings().first()
        assert (r["status"], r["retry_count"], r["assigned_model"]) == ("pending", 0, coder)
        assert r["tool_config"]["model_switched_from"] == general
        async with async_session() as db:              # exhausted again on the new model: never a second switch
            await db.execute(text("UPDATE dag_nodes SET status = 'failed', retry_count = 3 WHERE job_id = :j"),
                             {"j": jid})
            await db.commit()
        async with async_session() as db:
            again = await retry_failed_node(jid, "T6", db)
        assert "model_switch" not in again
    finally:
        await _drop(jid)


@pytest.mark.integration
async def test_refusals_of_different_kinds_do_not_switch():
    from app.database import async_session
    async with async_session() as db:
        jid = await _job(db, [FAKE.format(n=37), "tests failed: assert 0 == 1",
                              "  - `tests/t.py:9` deletes from `signal`, an append-only table: ..."],
                         get_model("model_general", None))
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T6", db)
        assert "model_switch" not in out
    finally:
        await _drop(jid)
