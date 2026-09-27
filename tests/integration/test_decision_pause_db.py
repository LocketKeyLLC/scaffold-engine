"""§17.1184 — the decision pause against real Postgres: the pending-decision
look-up, the claim exclusion, the jsonb park, the Alembic status constraint,
the resolve, and the run picking up where it stopped. LLM calls are patched;
everything else is production code on the real schema."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.config import settings
from app.modules import decision_pause as dp
from app.modules import execution_agent as ea

pytestmark = pytest.mark.asyncio


async def _seed(db_session, job_id):
    await db_session.execute(text("""
        INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, depends_on, execution_order, tool, prompt_template)
        VALUES (:j, 'T1', 'Gather facts', 'task', 'done', '{}', 0, 'LLM', 'facts'),
               (:j, 'T2', 'Decide GPU passthrough strategy', 'decision', 'pending', '{T1}', 1, 'LLM',
                'Operator decision: whole-GPU (suggested) vs Docker vs LXC.'),
               (:j, 'T3', 'Apply the chosen strategy', 'task', 'pending', '{T2}', 2, 'LLM', 'apply')
    """), {"j": job_id})
    await db_session.execute(text("UPDATE dag_nodes SET output_text = 'the P40 is in slot 2' WHERE job_id = :j AND node_key = 'T1'"), {"j": job_id})
    await db_session.commit()


async def _events(gen):
    out = []
    async for chunk in gen:
        for ln in chunk.splitlines():
            if ln.startswith("event: "):
                out.append(ln[7:])
    return out


def _frame(*_a, **_k):
    return {"question": "How should the P40 reach the AI workloads?", "detail": "",
            "options": [{"label": "Whole-GPU", "fit": "one VM", "tradeoff": "locks the card"},
                        {"label": "LXC", "fit": "many guests", "tradeoff": "weaker isolation"}],
            "suggested": "Whole-GPU", "why": "best documented", "framed": True}


async def _status(db_session, job_id):
    return (await db_session.execute(text("SELECT status, metadata FROM jobs WHERE id = :j"), {"j": job_id})).mappings().first()


@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
async def test_the_run_stops_at_the_decision_and_continues_on_the_answer(db_session, insert_job, monkeypatch, parallel):
    monkeypatch.setattr(settings, "decision_pause_enabled", True)
    monkeypatch.setattr(settings, "parallel_execution_enabled", parallel)
    monkeypatch.setattr(settings, "hands_on_assist_gate_enabled", False)
    job_id = await insert_job(status="executing", refined_brief={"title": "lab", "description": "home lab"})
    await _seed(db_session, job_id)

    executed: list[str] = []

    async def fake_exec(job_id_, model_overrides=None, preclaimed_node=None, **kw):
        node = preclaimed_node or await ea._get_next_node_for_test(job_id_)
        executed.append(node["node_key"])
        async with ea.async_session() as db:
            await db.execute(text("UPDATE dag_nodes SET status='done', output_text='ran', completed_at=NOW() "
                                  "WHERE job_id=:j AND node_key=:k"), {"j": job_id_, "k": node["node_key"]})
            await db.commit()
        return {"status": "done", "node_key": node["node_key"], "title": node["title"], "tool": "LLM", "output": "ran"}

    # 1. the run parks at T2 without claiming it
    with patch.object(dp, "frame_decision", AsyncMock(side_effect=_frame)), \
         patch.object(ea, "execute_next_node", AsyncMock(side_effect=fake_exec)), \
         patch.object(ea, "_get_next_node_for_test", _get_next_node_serial, create=True):
        events = await _events(ea.execute_all_nodes(job_id))
    assert events[-1] == "awaiting_decision", events
    assert executed == []
    row = await _status(db_session, job_id)
    md = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
    assert row["status"] == "awaiting_decision"              # the Alembic CHECK accepted it
    assert md["awaiting_decision"]["node_key"] == "T2" and md["awaiting_decision"]["suggested"] == "Whole-GPU"
    t2 = (await db_session.execute(text("SELECT status FROM dag_nodes WHERE job_id=:j AND node_key='T2'"), {"j": job_id})).scalar()
    assert t2 == "pending"

    # 2. the operator answers → T2 is done with the decision as its output, job back to executing
    async with ea.async_session() as db:
        out = await dp.resolve_decision(db, job_id, "T2", choice="LXC", note="isolation is fine here")
    assert out["outcome"] == "resolved"
    t2 = (await db_session.execute(text("SELECT status, output_text FROM dag_nodes WHERE job_id=:j AND node_key='T2'"), {"j": job_id})).mappings().first()
    assert t2["status"] == "done" and t2["output_text"].startswith("Decision (operator): LXC")
    row = await _status(db_session, job_id)
    md = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
    assert row["status"] == "executing" and "awaiting_decision" not in md and md["decisions"]["T2"]["by"] == "operator"

    # 3. the restarted run executes T3 (which reads the decision upstream) and completes
    with patch.object(dp, "frame_decision", AsyncMock(side_effect=_frame)), \
         patch.object(ea, "execute_next_node", AsyncMock(side_effect=fake_exec)), \
         patch.object(ea, "_get_next_node_for_test", _get_next_node_serial, create=True), \
         patch.object(ea, "_build_pipeline_summary", AsyncMock(return_value={"status": "completed", "total_nodes": 3, "passed": 3, "failed": 0})), \
         patch.object(ea, "_flip_job_completed", AsyncMock(return_value=True)):
        events = await _events(ea.execute_all_nodes(job_id))
    assert executed == ["T3"], executed
    assert "awaiting_decision" not in events


async def test_a_delegated_decision_is_claimed_and_run(db_session, insert_job, monkeypatch):
    monkeypatch.setattr(settings, "decision_pause_enabled", True)
    monkeypatch.setattr(settings, "parallel_execution_enabled", True)
    monkeypatch.setattr(settings, "hands_on_assist_gate_enabled", False)
    job_id = await insert_job(status="executing", refined_brief={"title": "lab", "description": "home lab"})
    await _seed(db_session, job_id)
    await db_session.execute(text("UPDATE jobs SET metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:p AS jsonb) WHERE id=:j"),
                             {"j": job_id, "p": json.dumps({"decisions": {"T2": {"by": "engine"}}})})
    await db_session.commit()
    async with ea.async_session() as db:
        assert await dp.pending_decision(db, job_id) is None
        claimed = await ea._claim_ready_nodes(db, job_id, 4)
    assert [c["node_key"] for c in claimed] == ["T2"]


async def test_an_undelegated_decision_is_never_claimed_by_the_frontier(db_session, insert_job, monkeypatch):
    monkeypatch.setattr(settings, "decision_pause_enabled", True)
    job_id = await insert_job(status="executing")
    await _seed(db_session, job_id)
    async with ea.async_session() as db:
        claimed = await ea._claim_ready_nodes(db, job_id, 4)
        pend = await dp.pending_decision(db, job_id)
    assert claimed == [] and pend["node_key"] == "T2"


async def _get_next_node_serial(job_id):
    async with ea.async_session() as db:
        return await ea._get_next_node(db, job_id)
