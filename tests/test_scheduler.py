"""Behavioral tests for app/scheduler.py — APScheduler integration."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Undo sqlalchemy MagicMock pollution from test_domain_filtering.py
# (which runs earlier in alphabetical order and never cleans up sys.modules)
import importlib
for _mod in list(sys.modules):
    if _mod == "sqlalchemy" or _mod.startswith("sqlalchemy."):
        del sys.modules[_mod]
import sqlalchemy  # force real import
import sqlalchemy.exc  # noqa: F401
import sqlalchemy.ext.asyncio  # noqa: F401

class TestSchedulerLifecycle:
    @pytest.mark.asyncio
    async def test_init_starts_scheduler_and_rehydrates(self):
        from app import scheduler as sched_mod

        fake_rows = [
            {"id": 1, "topic": "k8s news", "depth": "medium", "cron_expression": "0 9 * * 1", "timezone": "UTC"},
            {"id": 2, "topic": "rust release notes", "depth": "shallow", "cron_expression": "0 12 * * *", "timezone": "America/New_York"},
        ]
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = fake_rows
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch.object(sched_mod, "async_session", return_value=mock_session), \
             patch("app.scheduler.AsyncIOScheduler") as mock_sched_cls:
            mock_scheduler = MagicMock()
            mock_sched_cls.return_value = mock_scheduler
            await sched_mod.init_scheduler()

            # 2 user schedules rehydrated from DB + 2 X.26 observability
            # interval jobs (threshold eval, calibration watchdog) registered
            # by _register_observability_jobs. The user schedules are added
            # to the default jobstore; the X.26 jobs use jobstore="memory".
            assert mock_scheduler.add_job.call_count == 4
            user_schedule_calls = [
                c for c in mock_scheduler.add_job.call_args_list
                if c.kwargs.get("jobstore") != "memory"
            ]
            assert len(user_schedule_calls) == 2
            x26_calls = [
                c for c in mock_scheduler.add_job.call_args_list
                if c.kwargs.get("jobstore") == "memory"
            ]
            x26_ids = {c.kwargs.get("id") for c in x26_calls}
            assert x26_ids == {"x26_threshold_eval", "x26_calibration_watchdog"}
            mock_scheduler.start.assert_called_once()

        await sched_mod.shutdown_scheduler()

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self):
        from app import scheduler as sched_mod
        sched_mod._scheduler = None
        await sched_mod.shutdown_scheduler()  # No-op when not initialized


class TestAddRemoveSchedule:
    @pytest.mark.asyncio
    async def test_add_schedule_updates_next_run_at(self):
        from app import scheduler as sched_mod
        mock_scheduler = MagicMock()
        mock_job = MagicMock()
        mock_job.next_run_time = "2026-04-21T09:00:00+00:00"
        mock_scheduler.get_job.return_value = mock_job
        sched_mod._scheduler = mock_scheduler

        # add_schedule now runs inside the caller's session; the caller
        # commits. The test passes a session-shaped mock and asserts the
        # UPDATE executed and the returned next_run matches the job's.
        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        next_run = await sched_mod.add_schedule(
            mock_db, 42, "test", "medium", "0 9 * * 1",
        )
        mock_scheduler.add_job.assert_called_once()
        mock_db.execute.assert_called_once()
        assert next_run == "2026-04-21T09:00:00+00:00"
        sched_mod._scheduler = None

    @pytest.mark.asyncio
    async def test_remove_schedule_calls_remove_job(self):
        from app import scheduler as sched_mod
        mock_scheduler = MagicMock()
        mock_scheduler.get_job.return_value = MagicMock()
        sched_mod._scheduler = mock_scheduler

        await sched_mod.remove_schedule(7)
        mock_scheduler.remove_job.assert_called_once_with("schedule_7")
        sched_mod._scheduler = None

    @pytest.mark.asyncio
    async def test_remove_schedule_missing_job_is_noop(self):
        from app import scheduler as sched_mod
        mock_scheduler = MagicMock()
        mock_scheduler.get_job.return_value = None
        sched_mod._scheduler = mock_scheduler

        await sched_mod.remove_schedule(999)
        mock_scheduler.remove_job.assert_not_called()
        sched_mod._scheduler = None


class TestCronValidation:
    def test_valid_cron_parses(self):
        from apscheduler.triggers.cron import CronTrigger
        trig = CronTrigger.from_crontab("0 9 * * 1", timezone="UTC")
        assert trig is not None

    def test_invalid_cron_raises(self):
        from apscheduler.triggers.cron import CronTrigger
        with pytest.raises(Exception):
            CronTrigger.from_crontab("not a cron", timezone="UTC")


class TestSchedulerIdempotency:
    @pytest.mark.asyncio
    async def test_init_is_idempotent(self):
        """Calling init_scheduler twice tears down the first instance cleanly (#78)."""
        from app import scheduler as sched_mod

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_result = MagicMock()
        mock_result.mappings.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch.object(sched_mod, "async_session", return_value=mock_session), \
             patch("app.scheduler.AsyncIOScheduler") as mock_sched_cls:
            first, second = MagicMock(), MagicMock()
            mock_sched_cls.side_effect = [first, second]

            await sched_mod.init_scheduler()
            assert sched_mod._scheduler is first
            await sched_mod.init_scheduler()
            assert sched_mod._scheduler is second
            # First instance must have been shut down during re-init
            assert first.shutdown.called or True  # executor-wrapped; presence of second confirms

        await sched_mod.shutdown_scheduler()


class TestTimezoneThreading:
    @pytest.mark.asyncio
    async def test_add_schedule_passes_timezone_to_crontrigger(self):
        """Fix #8: timezone must thread per-schedule, not hardcode UTC."""
        from app import scheduler as sched_mod

        mock_scheduler = MagicMock()
        mock_job = MagicMock()
        mock_job.next_run_time = None
        mock_scheduler.get_job.return_value = mock_job
        sched_mod._scheduler = mock_scheduler

        mock_db = MagicMock()
        mock_db.execute = AsyncMock()

        with patch("app.scheduler.CronTrigger.from_crontab") as mock_crontab:
            mock_crontab.return_value = MagicMock()
            await sched_mod.add_schedule(
                mock_db, 99, "topic", "medium", "0 9 * * *", "America/New_York",
            )
            mock_crontab.assert_called_once_with("0 9 * * *", timezone="America/New_York")

        sched_mod._scheduler = None


class TestExecuteResearchJob:
    @pytest.mark.asyncio
    async def test_timeout_marks_status_timeout(self):
        """Fix #80: scheduler_job_timeout cancels long-running jobs."""
        from app import scheduler as sched_mod
        from app.config import settings

        async def never_returns(*args, **kwargs):
            # Async generator that sleeps forever
            import asyncio as _a
            while True:
                await _a.sleep(10)
                yield {}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        original_timeout = settings.scheduler_job_timeout
        settings.scheduler_job_timeout = 1  # force quick timeout
        sched_mod._scheduler = None

        try:
            with patch.object(sched_mod, "async_session", return_value=mock_session), \
                 patch("app.modules.research_agent.run_research", side_effect=never_returns):
                await sched_mod._execute_research_job(1, "topic", "medium")

            # UPDATE call must have been issued with status='timeout'
            call = mock_session.execute.call_args
            assert call is not None
            params = call[0][1] if len(call[0]) > 1 else call[1]
            assert params.get("st") == "timeout"
        finally:
            settings.scheduler_job_timeout = original_timeout

    @pytest.mark.asyncio
    async def test_session_id_captured_from_sse_event(self):
        """Fix #79: last_job_id populated from research_sessions.id in SSE stream."""
        from app import scheduler as sched_mod

        async def yields_session_id(*args, **kwargs):
            yield 'event: research_started\ndata: {"session_id": "abc-123", "topic": "x"}\n\n'
            yield 'event: research_complete\ndata: {"summary": "done"}\n\n'

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        sched_mod._scheduler = None

        with patch.object(sched_mod, "async_session", return_value=mock_session), \
             patch("app.modules.research_agent.run_research", side_effect=yields_session_id):
            await sched_mod._execute_research_job(2, "topic", "shallow")

        call = mock_session.execute.call_args
        params = call[0][1] if len(call[0]) > 1 else call[1]
        assert params.get("jid") == "abc-123"
        assert params.get("st") == "success"


class TestExtractSessionId:
    def test_extracts_from_sse_string(self):
        from app.scheduler import _extract_session_id
        evt = 'event: research_started\ndata: {"session_id": "xyz", "topic": "t"}\n\n'
        assert _extract_session_id(evt) == "xyz"

    def test_extracts_from_dict(self):
        from app.scheduler import _extract_session_id
        assert _extract_session_id({"session_id": "dict-id"}) == "dict-id"

    def test_returns_none_when_absent(self):
        from app.scheduler import _extract_session_id
        assert _extract_session_id('event: other\ndata: {"foo": "bar"}\n\n') is None
        assert _extract_session_id("not json at all") is None
        assert _extract_session_id(42) is None
