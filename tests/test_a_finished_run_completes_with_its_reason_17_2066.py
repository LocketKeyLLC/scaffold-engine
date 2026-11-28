"""§17.2066 — a run that finds every step finished completes the job itself, and says why.

Live (2026-11-28, job 0fb4ea7f, RD83 then RDV83 -- two ordinary approved redeploys, each the job's last step): the
approval ran the step through `resolve_run`, `decide` restarted the run, and the run (the parallel frontier) claimed
nothing, saw every step finished and emitted `pipeline_complete` -- `pipeline_completed(parallel): ... total=0`. Nobody
had completed the job: the frontier's terminal assumed the LAST WORKER's autocomplete had, and here no worker ran. The
job was still `running`, so `_cleanup_stuck_running_job` got `exit_reason=None`, took its "safety default" (`failed`),
§17.1492 upgraded it to `completed`, and it logged `execute_all_nodes_cleanup: job=... running->completed
(reason=None)` at WARNING -- the normal path dressed as an anomaly, and the deliverable never recompiled.

These drive the PRODUCTION `_run_parallel_frontier` terminal (claim, pause check, all-done, summary faked at their
edges; the finisher `_complete_finished_job` and `_flip_job_completed` real against a fake session that holds the job's
status) and the PRODUCTION `_cleanup_stuck_running_job` through the REAL `job_state.transition`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.modules import execution_agent as ea

pytestmark = pytest.mark.smoke

JID = "00000000-0000-0000-0000-0000000020a6"


class _Job:
    """The job row the fake sessions share: its status and every statement sent."""

    def __init__(self, status: str, open_steps: int = 0, total_steps: int = 2):
        self.status, self.open, self.total, self.sql = status, open_steps, total_steps, []


class _Session:
    def __init__(self, job: _Job):
        self.job = job

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.job.sql.append(sql)
        job = self.job
        moved = None
        if sql.startswith("UPDATE jobs SET status = 'completed'"):                     # _flip_job_completed
            if job.status not in ("completed", "cancelled", "failed"):
                job.status, moved = "completed", ("id",)
        elif sql.startswith("UPDATE jobs SET status = :to"):                            # job_state.transition
            if "IN ('running')" in sql and job.status == "running":
                moved, job.status = (job.status,), params["to"]

        class R:
            rowcount = 1

            def fetchone(self_inner):
                return moved

            def first(self_inner):
                return moved

            def scalar(self_inner):
                return job.status if sql.startswith("SELECT status") else None      # the refusal's read; no grounding

            def mappings(self_inner):
                class M:
                    def first(self2):
                        return {"open": job.open, "total": job.total}
                return M()
        return R()

    async def commit(self):
        self.job.sql.append("COMMIT")

    async def rollback(self):
        pass


def _levels(caplog, prefix):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.getMessage().startswith(prefix)]


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


async def _frontier(monkeypatch, job: _Job) -> list[str]:
    """The run `decide` restarts after the approval settled the last of the DAG's two steps: nothing to claim."""
    monkeypatch.setattr(ea, "async_session", lambda: _Session(job))
    monkeypatch.setattr(ea, "_claim_ready_nodes", AsyncMock(return_value=[]))
    monkeypatch.setattr(ea, "_pause_for_decision", AsyncMock(return_value=None))
    monkeypatch.setattr(ea, "_all_nodes_done", AsyncMock(return_value=True))      # T1 done, T2 done (the approval)
    monkeypatch.setattr(ea, "_make_dag_progress_tracker", AsyncMock(return_value=None))
    monkeypatch.setattr(ea, "_build_pipeline_summary", AsyncMock(
        return_value={"status": "completed", "total_nodes": 0, "passed": 0, "failed": 0}))
    compile_ = AsyncMock(return_value=("## T1\nran\n## T2\nredeployed", False))
    monkeypatch.setattr(ea, "_compile_output", compile_)
    monkeypatch.setattr(ea, "compute_deliverable_kind", AsyncMock(return_value="report"))
    monkeypatch.setattr(ea, "persist_job_artifacts", AsyncMock(return_value=None))
    monkeypatch.setattr(ea, "execute_next_node", AsyncMock(side_effect=AssertionError("no step is run")))
    monkeypatch.setattr(settings, "sse_keepalive_seconds", 0.2)
    events = [ev async for ev in ea._run_parallel_frontier(JID, model_overrides=None, t0=time.monotonic(),
                                                            retry_budget=3)]
    return events, compile_


@pytest.mark.asyncio
async def test_a_run_with_nothing_left_completes_the_job_itself_and_names_why(monkeypatch, caplog):
    job = _Job("running")
    with caplog.at_level(logging.DEBUG):
        events, compile_ = await _frontier(monkeypatch, job)
    assert any(e.startswith(ea._PIPELINE_COMPLETE_FRAME) for e in events), events
    assert job.status == "completed", "the run completed the job -- the cleanup's fallback is not needed"
    compile_.assert_awaited_once()                                        # ... and the deliverable is compiled
    assert any("compiled_output = :co" in s for s in job.sql)
    finished = _levels(caplog, "job_finished:")
    assert finished == [(logging.INFO, f"job_finished: job={JID} reason=no_step_left")], finished
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_job_a_worker_already_completed_is_not_completed_twice(monkeypatch, caplog):
    job = _Job("completed")
    with caplog.at_level(logging.DEBUG):
        events, compile_ = await _frontier(monkeypatch, job)
    assert any(e.startswith(ea._PIPELINE_COMPLETE_FRAME) for e in events)
    assert job.status == "completed"
    compile_.assert_not_awaited()
    assert _levels(caplog, "job_finished:") == []
    assert _warnings(caplog) == []


def _cleanup(monkeypatch, caplog, job: _Job, exit_reason):
    monkeypatch.setattr(ea, "async_session", lambda: _Session(job))
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(ea._cleanup_stuck_running_job(JID, exit_reason))
    return _levels(caplog, "execute_all_nodes_cleanup:")


def test_a_finished_run_still_running_is_completed_at_info_with_its_reason(monkeypatch, caplog):
    job = _Job("running", open_steps=0, total_steps=2)
    logs = _cleanup(monkeypatch, caplog, job, "finished")
    assert job.status == "completed"
    assert logs == [(logging.INFO, f"execute_all_nodes_cleanup: job={JID} running->completed (reason=finished)")], logs
    assert _warnings(caplog) == []
    assert any("reason=execute_all_nodes_cleanup:finished" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("exit_reason, open_steps, terminal", [
    (None, 1, "failed"),          # the safety default: an unexplained return that left a step open
    ("finished", 1, "failed"),    # "finished" with a step open is not finished
    (None, 0, "completed"),       # §17.1492's upgrade holds, but a run that returned without saying why is flagged
    ("exception", 0, "failed"),
    ("cancelled", 0, "cancelled"),
])
def test_the_genuine_safety_default_still_warns(monkeypatch, caplog, exit_reason, open_steps, terminal):
    job = _Job("running", open_steps=open_steps, total_steps=2)
    logs = _cleanup(monkeypatch, caplog, job, exit_reason)
    assert job.status == terminal
    assert logs == [(logging.WARNING,
                     f"execute_all_nodes_cleanup: job={JID} running->{terminal} (reason={exit_reason})")], logs


def test_a_job_the_run_already_completed_is_the_cleanups_quiet_no_op(monkeypatch, caplog):
    job = _Job("completed")
    logs = _cleanup(monkeypatch, caplog, job, "finished")
    assert logs == [] and job.status == "completed"
    assert _warnings(caplog) == []
