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
            {"id": 1, "topic": "k8s news", "depth": "medium", "cron_expression": "0 9 * * 1"},
            {"id": 2, "topic": "rust release notes", "depth": "shallow", "cron_expression": "0 12 * * *"},
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

            assert mock_scheduler.add_job.call_count == 2
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

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()

        with patch.object(sched_mod, "async_session", return_value=mock_session):
            await sched_mod.add_schedule(42, "test", "medium", "0 9 * * 1")
            mock_scheduler.add_job.assert_called_once()
            mock_session.execute.assert_called_once()
            mock_session.commit.assert_called_once()
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
