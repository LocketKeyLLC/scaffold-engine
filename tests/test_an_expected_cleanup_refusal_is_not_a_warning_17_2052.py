"""§17.2052 — a routine guarded no-op is not logged as a warning.

Live (2026-11-26, job 0fb4ea7f, the server-redeploy job): `_cleanup_stuck_running_job` runs after every execute_all
run. When the run parked for approval the job is `awaiting_decision`, the cleanup's guarded `running -> failed` claim
is refused -- exactly the no-op its docstring promises -- and `job_transition_refused: ... to=failed
current=awaiting_decision guard=jobs.status IN ('running') reason=execute_all_nodes_cleanup:None` was logged at
WARNING seven times that day, one per park, reading as a fault in every triage.

These drive the PRODUCTION cleanup through the REAL `job_state.transition` (only the session is faked): a refusal
from a status the run legitimately ends in is INFO; a refusal from anything else is still a WARNING; and a job that
really is stuck at `running` still moves.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.modules import execution_agent as ea
from app.modules import job_state
from app.modules.job_state import transition

JID = "00000000-0000-0000-0000-00000000c1ea"


class _Session:
    """`first()` is what the guarded UPDATE returns (None = refused); `scalar()` the refusal's status read;
    `mappings().first()` the cleanup's open-node count."""

    def __init__(self, current: str | None, moved: bool = False):
        self.current, self.moved, self.sql = current, moved, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        self.sql.append(" ".join(str(stmt).split()))
        outer = self

        class R:
            rowcount = 1

            def first(self_inner):
                return ("running",) if outer.moved else None

            def scalar(self_inner):
                return outer.current

            def mappings(self_inner):
                class M:
                    def first(self2):
                        return {"open": 3, "total": 10}
                return M()
        return R()

    async def commit(self):
        self.sql.append("COMMIT")


def _cleanup(monkeypatch, caplog, sess):
    monkeypatch.setattr(ea, "async_session", lambda: sess)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="scaffold"):
        asyncio.run(ea._cleanup_stuck_running_job(JID, None))
    return [(r.levelno, r.getMessage()) for r in caplog.records]


@pytest.mark.parametrize("current", ["awaiting_decision", "blocked", "completed", "cancelled"])
def test_the_cleanups_expected_refusal_is_info_not_a_warning(monkeypatch, caplog, current):
    logs = _cleanup(monkeypatch, caplog, _Session(current))
    warn = [m for lvl, m in logs if lvl >= logging.WARNING]
    assert not warn, warn
    info = [m for lvl, m in logs if lvl == logging.INFO and "job_transition_not_needed" in m]
    assert len(info) == 1 and f"current={current}" in info[0] and "execute_all_nodes_cleanup:None" in info[0], logs


@pytest.mark.parametrize("current", ["executing", None])
def test_an_unexpected_refusal_still_warns(monkeypatch, caplog, current):
    """A job the cleanup finds `executing` (or whose row vanished) is not a run that ended the way runs end."""
    logs = _cleanup(monkeypatch, caplog, _Session(current))
    warn = [m for lvl, m in logs if lvl >= logging.WARNING and "job_transition_refused" in m]
    assert len(warn) == 1 and f"current={current}" in warn[0], logs


def test_a_job_really_stuck_at_running_still_moves(monkeypatch, caplog):
    sess = _Session("running", moved=True)
    logs = _cleanup(monkeypatch, caplog, sess)
    assert any("UPDATE dag_nodes SET status = 'failed'" in s for s in sess.sql)
    assert any("execute_all_nodes_cleanup: job=" in m and "running->failed" in m for _l, m in logs), logs


@pytest.mark.asyncio
async def test_other_callers_keep_their_warning_and_the_names_are_validated(caplog):
    """No caller that did not declare a refusal expected is quieted -- and a misspelled status is refused loudly."""
    with caplog.at_level(logging.INFO, logger="scaffold.job_state"):
        assert await transition(_Session("awaiting_decision"), JID, to="failed", expected_from=("running",),
                                reason="x") is None
    assert [r.levelno for r in caplog.records if "job_transition_refused" in r.getMessage()] == [logging.WARNING]
    with pytest.raises(ValueError):
        await transition(_Session("blocked"), JID, to="failed", refusal_expected_when=("parked",))
    assert ea.CLEANUP_LEFT_RUNNING_STATUSES >= job_state.TERMINAL_JOB_STATUSES
    assert "running" not in ea.CLEANUP_LEFT_RUNNING_STATUSES and "executing" not in ea.CLEANUP_LEFT_RUNNING_STATUSES
