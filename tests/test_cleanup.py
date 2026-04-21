"""Tests for app/modules/cleanup.py (#9.30)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import cleanup


def _db_with_rowcounts(*rowcounts):
    """Build a db whose sequential execute() calls return mocks with given rowcount."""
    db = AsyncMock()
    results = []
    for rc in rowcounts:
        r = MagicMock()
        r.rowcount = rc
        results.append(r)
    db.execute.side_effect = results
    return db


async def test_reap_stale_jobs_returns_all_four_counts():
    """The function always returns a dict with 4 category keys."""
    db = _db_with_rowcounts(2, 1, 3, 0)
    result = await cleanup.reap_stale_jobs(db)
    assert set(result.keys()) == {
        "running_to_failed", "planning_to_cancelled",
        "research_to_failed", "paused_to_cancelled",
    }
    assert result["running_to_failed"] == 2
    assert result["planning_to_cancelled"] == 1
    assert result["research_to_failed"] == 3
    assert result["paused_to_cancelled"] == 0
    db.commit.assert_awaited()


async def test_reap_stale_jobs_runs_four_sql_statements():
    """Exactly 4 UPDATE ... RETURNING statements, one per category."""
    db = _db_with_rowcounts(0, 0, 0, 0)
    await cleanup.reap_stale_jobs(db)
    assert db.execute.await_count == 4


async def test_reap_stale_jobs_no_reaping_returns_zero_counts():
    db = _db_with_rowcounts(0, 0, 0, 0)
    result = await cleanup.reap_stale_jobs(db)
    assert all(v == 0 for v in result.values())


async def test_start_cleanup_task_registers_strong_reference():
    """#7.4 / #9.30: task must live in _background_tasks to avoid GC."""
    with patch.object(cleanup, "_cleanup_loop") as loop:
        # Make _cleanup_loop an awaitable that never starts
        async def _noop():
            import asyncio
            await asyncio.sleep(3600)
        loop.side_effect = _noop
        task = cleanup.start_cleanup_task()
        try:
            assert task in cleanup._background_tasks
            assert task.get_name() == "stale-job-cleanup"
        finally:
            task.cancel()
            try:
                await task
            except BaseException:
                pass


def test_cleanup_interval_is_15_minutes():
    assert cleanup.CLEANUP_INTERVAL_SECONDS == 900  # 15 * 60


def test_stale_threshold_is_30_minutes():
    assert cleanup.STALE_THRESHOLD_MINUTES == 30
