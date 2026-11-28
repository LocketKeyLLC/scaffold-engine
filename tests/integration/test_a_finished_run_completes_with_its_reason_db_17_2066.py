"""§17.2066 — against real Postgres: the run `decide` restarts after an approval settled the LAST step completes the
job itself, names why, and leaves the cleanup nothing to fix -- serial and parallel.

Live (2026-11-28, job 0fb4ea7f RD83/RDV83): the parallel frontier found nothing to claim, emitted `pipeline_complete`
and left the job `running`; the cleanup's safety default completed it with `reason=None` at WARNING and no deliverable
was compiled. Everything here is production code on the real schema; only the compile's model call is patched.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.config import settings
from app.modules import execution_agent as ea

pytestmark = pytest.mark.asyncio


async def _seed(db_session, job_id):
    """T1 ran; RD83 (the approved redeploy) was settled `done` by resolve_run before decide restarted the run."""
    await db_session.execute(text("""
        INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, depends_on, execution_order, tool,
                               prompt_template, output_text, completed_at)
        VALUES (:j, 'T1', 'Read the engine version', 'task', 'done', '{}', 0, 'LLM', 'read', 'v1.9', NOW()),
               (:j, 'RD83', 'VM 110: redeploy the engine at main', 'task', 'done', '{T1}', 1, 'Shell', 'redeploy',
                'deployed 1a2b3c4d', NOW())
    """), {"j": job_id})
    await db_session.commit()


async def _drain(gen):
    return [ln[7:] for chunk in [c async for c in gen] for ln in chunk.splitlines() if ln.startswith("event: ")]


@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
async def test_the_restarted_run_completes_the_job_with_its_reason(db_session, insert_job, monkeypatch, caplog,
                                                                   parallel):
    monkeypatch.setattr(settings, "parallel_execution_enabled", parallel)
    monkeypatch.setattr(settings, "hands_on_assist_gate_enabled", False)
    job_id = await insert_job(status="executing", refined_brief={"title": "redeploy", "description": "engine"})
    await _seed(db_session, job_id)

    reasons: list = []
    spawn = ea._spawn_cleanup_task

    def _spy(jid, exit_reason):
        reasons.append(exit_reason)
        return spawn(jid, exit_reason)

    with patch.object(ea, "_compile_output", AsyncMock(return_value=("## RD83\ndeployed 1a2b3c4d", False))), \
         patch.object(ea, "_spawn_cleanup_task", _spy), caplog.at_level(logging.DEBUG):
        events = await _drain(ea.execute_all_nodes(job_id))
        await ea.drain_cleanup_tasks()

    assert events[-1] == "pipeline_complete", events
    row = (await db_session.execute(text("SELECT status, compiled_output FROM jobs WHERE id = :j"),
                                    {"j": job_id})).mappings().first()
    assert row["status"] == "completed" and row["compiled_output"] == "## RD83\ndeployed 1a2b3c4d", dict(row)
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (logging.INFO, f"job_finished: job={job_id} reason=no_step_left") in msgs, msgs
    assert not [m for lvl, m in msgs if lvl >= logging.WARNING and "execute_all_nodes_cleanup" in m], msgs
    assert not [m for lvl, m in msgs if lvl >= logging.WARNING and job_id in m], msgs
    assert reasons == ["finished"], reasons                    # the run named how it ended
