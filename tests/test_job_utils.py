"""§17.185 — unit tests for app/utils/job_utils.py.

The audit (AUDIT.md 3.2) flagged this module as untested despite being a
shared SQL helper imported across the orchestrator (dag_generator,
execution_agent, idea_refinement — every path that needs to fail a job
goes through ``fail_job``). It's a small module (one helper) but the
contract is load-bearing:

  * UPDATE jobs SET status='failed', error_summary=<truncated> WHERE id=:id
    AND status NOT IN (terminal)  — §17.1107: routed through
    app.modules.job_state.transition(); a completed/cancelled/failed job is
    never overwritten, and the helper reports whether the row moved
  * COMMIT (so the failed state survives any subsequent rollback)
  * Truncates error_summary to 1000 chars so a long traceback doesn't
    inflate every failed job row.

Tests below pin the truncation length, the SQL shape, the commit, and the
accepted job_id types (UUID + str).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from app.utils import job_utils as ju


# ---------------------------------------------------------------------------
# fail_job
# ---------------------------------------------------------------------------

class TestFailJob:
    @staticmethod
    def _db(moved=True):
        """A session whose UPDATE … RETURNING yields a prior row (moved) or
        nothing (guard refused); the refusal path then SELECTs the current
        status."""
        db = AsyncMock()
        res = MagicMock()
        res.first.return_value = ("planning",) if moved else None
        res.scalar.return_value = "cancelled"
        db.execute = AsyncMock(return_value=res)
        return db

    @staticmethod
    def _update_call(db):
        sql_obj, params = db.execute.await_args_list[0].args
        return " ".join(str(sql_obj).split()), params

    async def test_executes_guarded_update_with_failed_status(self):
        db = self._db()
        await ju.fail_job(db, "job-123", "something went wrong")
        sql, params = self._update_call(db)
        assert "UPDATE jobs" in sql
        assert "status = :to" in sql and params["to"] == "failed"
        assert "NOT IN ('cancelled', 'completed', 'failed')" in sql, "§17.1107 guard"
        assert params["id"] == "job-123"
        assert params["set_error_summary"] == "something went wrong"

    async def test_returns_true_when_moved_false_when_terminal(self):
        assert await ju.fail_job(self._db(moved=True), "job-1", "err") is True
        db = self._db(moved=False)
        assert await ju.fail_job(db, "job-1", "err") is False
        # refusal path: UPDATE + the diagnostic SELECT, and still a COMMIT
        assert db.execute.await_count == 2
        db.commit.assert_awaited_once()

    async def test_commits_after_update(self):
        """The helper MUST commit so the failed status survives a caller's
        subsequent rollback — load-bearing for the execution_agent's
        exception-handling boundary."""
        db = self._db()
        await ju.fail_job(db, "job-1", "err")
        db.commit.assert_awaited_once()

    async def test_truncates_error_to_cap(self):
        """error_summary is capped at _ERROR_SUMMARY_MAX (1000) so a long
        traceback doesn't bloat every failed job row."""
        db = self._db()
        await ju.fail_job(db, "job-1", "x" * 2500)
        _, params = self._update_call(db)
        assert len(params["set_error_summary"]) == ju._ERROR_SUMMARY_MAX
        assert params["set_error_summary"] == "x" * ju._ERROR_SUMMARY_MAX

    async def test_short_error_passes_through_untruncated(self):
        db = self._db()
        await ju.fail_job(db, "job-1", "short")
        assert self._update_call(db)[1]["set_error_summary"] == "short"

    async def test_accepts_uuid_job_id(self):
        """Both UUID and str job_id forms are accepted — the transition binds
        the id as text either way."""
        db = self._db()
        jid = uuid4()
        await ju.fail_job(db, jid, "boom")
        assert self._update_call(db)[1]["id"] == str(jid)

    async def test_accepts_string_job_id(self):
        db = self._db()
        jid_str = str(uuid4())
        await ju.fail_job(db, jid_str, "boom")
        assert self._update_call(db)[1]["id"] == jid_str

    async def test_empty_error_message_still_executes(self):
        """An empty error summary is unusual but must not crash — callers
        sometimes pass through whatever str(exc) yields."""
        db = self._db()
        await ju.fail_job(db, "j1", "")
        assert self._update_call(db)[1]["set_error_summary"] == ""

    async def test_error_at_exact_cap_not_modified(self):
        """An error of length == cap is the boundary — should not be
        truncated, only longer values are."""
        db = self._db()
        at_cap = "z" * ju._ERROR_SUMMARY_MAX
        await ju.fail_job(db, "j1", at_cap)
        assert self._update_call(db)[1]["set_error_summary"] == at_cap

    async def test_logger_emits_on_fail(self, caplog):
        """Failure events should always be logged so an operator tailing
        journald can see the failure without querying the DB."""
        import logging
        db = self._db()
        with caplog.at_level(logging.ERROR, logger="scaffold.jobs"):
            await ju.fail_job(db, "job-id-here", "the error text")
        assert any(
            "job_failed" in rec.message and "job-id-here" in rec.message
            and "the error text" in rec.message
            for rec in caplog.records
        )

    async def test_refusal_is_logged_not_raised(self, caplog):
        """§17.1107 — a late failure after an operator cancel must not flip
        cancelled → failed; the guard refuses and says so."""
        import logging
        db = self._db(moved=False)
        with caplog.at_level(logging.WARNING, logger="scaffold.job_state"):
            assert await ju.fail_job(db, "job-x", "late failure") is False
        assert any("job_transition_refused" in r.message and "current=cancelled" in r.message
                   for r in caplog.records)
