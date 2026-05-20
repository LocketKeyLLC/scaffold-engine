"""§17.185 — unit tests for app/utils/job_utils.py.

The audit (AUDIT.md 3.2) flagged this module as untested despite being a
shared SQL helper imported across the orchestrator (dag_generator,
execution_agent, idea_refinement — every path that needs to fail a job
goes through ``fail_job``). It's a small module (one helper) but the
contract is load-bearing:

  * UPDATE jobs SET status='failed', error_summary=<truncated> WHERE id=:id
  * COMMIT (so the failed state survives any subsequent rollback)
  * Truncates error_summary to 1000 chars so a long traceback doesn't
    inflate every failed job row.

Tests below pin the truncation length, the SQL shape, the commit, and the
accepted job_id types (UUID + str).
"""
from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from app.utils import job_utils as ju


# ---------------------------------------------------------------------------
# fail_job
# ---------------------------------------------------------------------------

class TestFailJob:
    async def test_executes_update_with_failed_status(self):
        db = AsyncMock()
        await ju.fail_job(db, "job-123", "something went wrong")
        db.execute.assert_awaited_once()
        sql_obj, params = db.execute.await_args.args
        assert "UPDATE jobs" in str(sql_obj)
        assert "status = 'failed'" in str(sql_obj)
        assert params["id"] == "job-123"
        assert params["error"] == "something went wrong"

    async def test_commits_after_update(self):
        """The helper MUST commit so the failed status survives a caller's
        subsequent rollback — load-bearing for the execution_agent's
        exception-handling boundary."""
        db = AsyncMock()
        await ju.fail_job(db, "job-1", "err")
        db.commit.assert_awaited_once()

    async def test_truncates_error_to_cap(self):
        """error_summary is capped at _ERROR_SUMMARY_MAX (1000) so a long
        traceback doesn't bloat every failed job row."""
        db = AsyncMock()
        long_err = "x" * 2500
        await ju.fail_job(db, "job-1", long_err)
        params = db.execute.await_args.args[1]
        assert len(params["error"]) == ju._ERROR_SUMMARY_MAX
        assert params["error"] == "x" * ju._ERROR_SUMMARY_MAX

    async def test_short_error_passes_through_untruncated(self):
        db = AsyncMock()
        await ju.fail_job(db, "job-1", "short")
        params = db.execute.await_args.args[1]
        assert params["error"] == "short"

    async def test_accepts_uuid_job_id(self):
        """Both UUID and str job_id forms are accepted — callers pass
        whichever form they have without forced conversion."""
        db = AsyncMock()
        jid = uuid4()
        await ju.fail_job(db, jid, "boom")
        params = db.execute.await_args.args[1]
        assert params["id"] == jid

    async def test_accepts_string_job_id(self):
        db = AsyncMock()
        jid_str = str(uuid4())
        await ju.fail_job(db, jid_str, "boom")
        params = db.execute.await_args.args[1]
        assert params["id"] == jid_str

    async def test_empty_error_message_still_executes(self):
        """An empty error summary is unusual but must not crash — callers
        sometimes pass through whatever str(exc) yields."""
        db = AsyncMock()
        await ju.fail_job(db, "j1", "")
        db.execute.assert_awaited_once()
        params = db.execute.await_args.args[1]
        assert params["error"] == ""

    async def test_error_at_exact_cap_not_modified(self):
        """An error of length == cap is the boundary — should not be
        truncated, only longer values are."""
        db = AsyncMock()
        at_cap = "z" * ju._ERROR_SUMMARY_MAX
        await ju.fail_job(db, "j1", at_cap)
        params = db.execute.await_args.args[1]
        assert params["error"] == at_cap

    async def test_logger_emits_on_fail(self, caplog):
        """Failure events should always be logged so an operator tailing
        journald can see the failure without querying the DB."""
        import logging
        db = AsyncMock()
        with caplog.at_level(logging.ERROR, logger="scaffold.jobs"):
            await ju.fail_job(db, "job-id-here", "the error text")
        assert any(
            "job_failed" in rec.message and "job-id-here" in rec.message
            and "the error text" in rec.message
            for rec in caplog.records
        )
