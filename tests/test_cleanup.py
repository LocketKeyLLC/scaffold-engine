"""Tests for app/modules/cleanup.py — state-aware reaper + settings-backed.

Shape: reap_stale_jobs() issues 5 UPDATE ... RETURNING statements and returns
a 5-key dict keyed by category. Row counts come from len(fetchall()) rather
than the driver-dependent `rowcount` attribute.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import cleanup


def _db_with_counts(*counts):
    """Build an AsyncMock db whose sequential execute() calls return results
    with the given len(fetchall()) values."""
    db = AsyncMock()
    results = []
    for c in counts:
        r = MagicMock()
        # fetchall() is sync on SQLAlchemy 2.x async Result
        r.fetchall.return_value = [object()] * c
        results.append(r)
    db.execute.side_effect = results
    return db


async def test_reap_stale_jobs_returns_all_five_counts():
    """The function always returns a dict with 5 category keys."""
    db = _db_with_counts(2, 4, 1, 3, 0)
    result = await cleanup.reap_stale_jobs(db)
    assert set(result.keys()) == {
        "running_to_failed",
        "long_phase_to_failed",
        "planning_to_cancelled",
        "research_to_failed",
        "paused_to_cancelled",
    }
    assert result["running_to_failed"] == 2
    assert result["long_phase_to_failed"] == 4
    assert result["planning_to_cancelled"] == 1
    assert result["research_to_failed"] == 3
    assert result["paused_to_cancelled"] == 0
    db.commit.assert_awaited()


async def test_reap_stale_jobs_runs_five_sql_statements():
    """Exactly 5 UPDATE ... RETURNING statements, one per category."""
    db = _db_with_counts(0, 0, 0, 0, 0)
    await cleanup.reap_stale_jobs(db)
    assert db.execute.await_count == 5


async def test_reap_stale_jobs_no_reaping_returns_zero_counts():
    db = _db_with_counts(0, 0, 0, 0, 0)
    result = await cleanup.reap_stale_jobs(db)
    assert all(v == 0 for v in result.values())


async def test_reap_stale_jobs_passes_threshold_params_from_settings():
    """Thresholds in bind params must come from settings, not module constants."""
    db = _db_with_counts(0, 0, 0, 0, 0)
    await cleanup.reap_stale_jobs(db)
    calls = db.execute.await_args_list
    # Running guard (call 1) — base threshold
    assert calls[0].args[1]["threshold_min"] == settings.stale_threshold_minutes
    # Long-phase guard (call 2) — elevated threshold
    assert calls[1].args[1]["threshold_min"] == settings.long_phase_stale_minutes
    # Planning sweep (call 3) — planning threshold
    assert calls[2].args[1]["threshold_min"] == settings.planning_stale_minutes
    # Research sessions (call 4) — base threshold
    assert calls[3].args[1]["threshold_min"] == settings.stale_threshold_minutes
    # Paused research (call 5) — no threshold_min param (expires_at driven)
    assert "threshold_min" not in calls[4].args[1]


async def test_start_cleanup_task_registers_strong_reference():
    """Task must live in _background_tasks to avoid GC."""
    with patch.object(cleanup, "_cleanup_loop") as loop:
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


async def test_cleanup_loop_runs_eager_sweep_before_sleep():
    """_cleanup_loop must call _run_once() once before entering asyncio.sleep."""
    import asyncio

    call_order = []

    async def fake_run_once():
        call_order.append("run_once")

    async def fake_sleep(_seconds):
        call_order.append("sleep")
        raise asyncio.CancelledError()

    with patch.object(cleanup, "_run_once", side_effect=fake_run_once), \
         patch.object(cleanup.asyncio, "sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await cleanup._cleanup_loop()

    # First event must be an eager run_once, before any sleep.
    assert call_order[0] == "run_once"
    assert "sleep" in call_order


def test_cleanup_settings_are_sourced_from_config():
    """Cleanup settings respect their bounds and maintain reaper invariants.
    Originally asserted hardcoded 15min/30min/45min/60min defaults. Those
    defaults are deliberately overridden in deployment .env to CPU-realistic
    values (e.g., 3600s / 1440min) for slow local hardware. April 26 2026
    rewrote this to assert the *invariants* that must hold across all
    valid configs — not specific numbers.
    """
    # All values are positive integers within Pydantic bounds
    assert settings.cleanup_interval_seconds > 0
    assert settings.stale_threshold_minutes > 0
    assert settings.long_phase_stale_minutes > 0
    assert settings.planning_stale_minutes > 0

    # Stale thresholds must exceed the cleanup interval so the reaper has
    # something to reap when it runs (else the loop spins on fresh jobs).
    cleanup_interval_min = settings.cleanup_interval_seconds / 60
    assert settings.stale_threshold_minutes >= cleanup_interval_min, (
        f"stale_threshold_minutes ({settings.stale_threshold_minutes}) must be "
        f">= cleanup_interval ({cleanup_interval_min} min)"
    )
    assert settings.long_phase_stale_minutes >= settings.stale_threshold_minutes
    assert settings.planning_stale_minutes >= settings.stale_threshold_minutes
