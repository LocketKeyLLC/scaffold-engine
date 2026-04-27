"""Tests for app/modules/cleanup.py — state-aware reaper + settings-backed.

Shape: reap_stale_jobs() runs Stage 0 orphan-node reset, then 5 UPDATE ...
RETURNING statements, returning a 6-key dict. When the orphan reaper finds
rows, an additional refresh-parent-jobs UPDATE fires between Stage 0 and the
running-job reaper. Row counts come from len(fetchall()) rather than the
driver-dependent `rowcount` attribute.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import cleanup


def _orphan_row(job_id="00000000-0000-0000-0000-000000000001", node_key="T1"):
    """Mock row with .job_id and .node_key attrs (Stage 0 reads both)."""
    row = MagicMock()
    row.job_id = job_id
    row.node_key = node_key
    return row


def _db_with_counts(*counts, orphan_rows=None):
    """Build an AsyncMock db whose sequential execute() calls return results
    with the given len(fetchall()) values.

    The first count is the orphan-node reset (Stage 0). If that count is
    nonzero, callers must pass orphan_rows= so Stage 0 can read row.job_id /
    row.node_key. A refresh-parent-jobs call is then injected between
    Stage 0 and the running-job reaper.
    """
    db = AsyncMock()
    results = []
    orphan_count = counts[0]
    if orphan_count:
        rows = orphan_rows if orphan_rows is not None else [
            _orphan_row() for _ in range(orphan_count)
        ]
        assert len(rows) == orphan_count, (
            "orphan_rows length must match counts[0]"
        )
        r0 = MagicMock()
        r0.fetchall.return_value = rows
        results.append(r0)
        # refresh_parent_jobs result — fetchall not consumed, return value
        # doesn't matter, but the call still happens.
        r_refresh = MagicMock()
        r_refresh.fetchall.return_value = []
        results.append(r_refresh)
    else:
        r0 = MagicMock()
        r0.fetchall.return_value = []
        results.append(r0)
    for c in counts[1:]:
        r = MagicMock()
        r.fetchall.return_value = [object()] * c
        results.append(r)
    db.execute.side_effect = results
    return db


async def test_reap_stale_jobs_returns_all_six_counts():
    """The function always returns a dict with 6 category keys (5 + orphan)."""
    db = _db_with_counts(0, 2, 4, 1, 3, 0)
    result = await cleanup.reap_stale_jobs(db)
    assert set(result.keys()) == {
        "orphan_nodes_reset",
        "running_to_failed",
        "long_phase_to_failed",
        "planning_to_cancelled",
        "research_to_failed",
        "paused_to_cancelled",
    }
    assert result["orphan_nodes_reset"] == 0
    assert result["running_to_failed"] == 2
    assert result["long_phase_to_failed"] == 4
    assert result["planning_to_cancelled"] == 1
    assert result["research_to_failed"] == 3
    assert result["paused_to_cancelled"] == 0
    db.commit.assert_awaited()


async def test_reap_stale_jobs_orphan_reset_count_propagates():
    """When Stage 0 finds orphans, count surfaces in the return dict."""
    db = _db_with_counts(
        2, 0, 0, 0, 0, 0,
        orphan_rows=[_orphan_row(node_key="T1"), _orphan_row(node_key="T2")],
    )
    result = await cleanup.reap_stale_jobs(db)
    assert result["orphan_nodes_reset"] == 2


async def test_reap_stale_jobs_runs_six_sql_statements():
    """Stage 0 orphan reaper + 5 category statements = 6 statements when no orphans."""
    db = _db_with_counts(0, 0, 0, 0, 0, 0)
    await cleanup.reap_stale_jobs(db)
    assert db.execute.await_count == 6


async def test_reap_stale_jobs_runs_seven_sql_statements_when_orphans_found():
    """With orphans, the refresh-parent-jobs UPDATE fires too: 7 statements total."""
    db = _db_with_counts(
        1, 0, 0, 0, 0, 0,
        orphan_rows=[_orphan_row()],
    )
    await cleanup.reap_stale_jobs(db)
    assert db.execute.await_count == 7


async def test_reap_stale_jobs_no_reaping_returns_zero_counts():
    db = _db_with_counts(0, 0, 0, 0, 0, 0)
    result = await cleanup.reap_stale_jobs(db)
    assert all(v == 0 for v in result.values())


async def test_reap_stale_jobs_passes_threshold_params_from_settings():
    """Thresholds in bind params must come from settings, not module constants."""
    db = _db_with_counts(0, 0, 0, 0, 0, 0)
    await cleanup.reap_stale_jobs(db)
    calls = db.execute.await_args_list
    # Stage 0 — orphan-node threshold
    assert calls[0].args[1]["threshold_min"] == settings.node_orphan_threshold_minutes
    # Running guard (call 2) — base threshold
    assert calls[1].args[1]["threshold_min"] == settings.stale_threshold_minutes
    # Long-phase guard (call 3) — elevated threshold
    assert calls[2].args[1]["threshold_min"] == settings.long_phase_stale_minutes
    # Planning sweep (call 4) — planning threshold
    assert calls[3].args[1]["threshold_min"] == settings.planning_stale_minutes
    # Research sessions (call 5) — base threshold
    assert calls[4].args[1]["threshold_min"] == settings.stale_threshold_minutes
    # Paused research (call 6) — no threshold_min param (expires_at driven)
    assert "threshold_min" not in calls[5].args[1]


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
