"""Tests for app/modules/cleanup.py — state-aware reaper + settings-backed.

Shape: reap_stale_jobs() runs Stage 0 orphan-node reset, then 7 UPDATE ...
RETURNING statements (running, long_phase, planning, awaiting_confirmation,
research_sessions, paused_research, assist_abandoned), returning an 8-key
dict. When the orphan reaper finds rows, an additional refresh-parent-jobs
UPDATE fires between Stage 0 and the running-job reaper. Row counts come
from len(fetchall()) rather than the driver-dependent `rowcount` attribute.
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

    Expects exactly 8 counts: (orphan, running, long_phase, planning,
    awaiting_confirmation, research_sessions, paused_research,
    assist_abandoned). If the orphan count is nonzero, callers must pass
    orphan_rows= so Stage 0 can read row.job_id / row.node_key. A
    refresh-parent-jobs call is then injected between Stage 0 and the
    running-job reaper.
    """
    assert len(counts) == 8, (
        f"_db_with_counts expects 8 counts (orphan + 7 reapers), got {len(counts)}"
    )
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


async def test_reap_stale_jobs_returns_all_eight_counts():
    """The function always returns a dict with 8 category keys (7 + orphan)."""
    db = _db_with_counts(0, 2, 4, 1, 5, 3, 0, 6)
    result = await cleanup.reap_stale_jobs(db)
    assert set(result.keys()) == {
        "orphan_nodes_reset",
        "running_to_failed",
        "long_phase_to_failed",
        "planning_to_cancelled",
        "awaiting_to_cancelled",
        "research_to_failed",
        "paused_to_cancelled",
        "assist_abandoned",
    }
    assert result["orphan_nodes_reset"] == 0
    assert result["running_to_failed"] == 2
    assert result["long_phase_to_failed"] == 4
    assert result["planning_to_cancelled"] == 1
    assert result["awaiting_to_cancelled"] == 5
    assert result["research_to_failed"] == 3
    assert result["paused_to_cancelled"] == 0
    assert result["assist_abandoned"] == 6
    db.commit.assert_awaited()


async def test_reap_stale_jobs_orphan_reset_count_propagates():
    """When Stage 0 finds orphans, count surfaces in the return dict."""
    db = _db_with_counts(
        2, 0, 0, 0, 0, 0, 0, 0,
        orphan_rows=[_orphan_row(node_key="T1"), _orphan_row(node_key="T2")],
    )
    result = await cleanup.reap_stale_jobs(db)
    assert result["orphan_nodes_reset"] == 2


async def test_reap_stale_jobs_runs_eight_sql_statements():
    """Stage 0 orphan reaper + 7 category statements = 8 statements when no orphans."""
    db = _db_with_counts(0, 0, 0, 0, 0, 0, 0, 0)
    await cleanup.reap_stale_jobs(db)
    assert db.execute.await_count == 8


async def test_reap_stale_jobs_runs_nine_sql_statements_when_orphans_found():
    """With orphans, the refresh-parent-jobs UPDATE fires too: 9 statements total."""
    db = _db_with_counts(
        1, 0, 0, 0, 0, 0, 0, 0,
        orphan_rows=[_orphan_row()],
    )
    await cleanup.reap_stale_jobs(db)
    assert db.execute.await_count == 9


async def test_reap_stale_jobs_no_reaping_returns_zero_counts():
    db = _db_with_counts(0, 0, 0, 0, 0, 0, 0, 0)
    result = await cleanup.reap_stale_jobs(db)
    assert all(v == 0 for v in result.values())


async def test_reap_stale_jobs_passes_threshold_params_from_settings():
    """Thresholds in bind params must come from settings, not module constants."""
    db = _db_with_counts(0, 0, 0, 0, 0, 0, 0, 0)
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
    # Awaiting-confirmation sweep (call 5) — gate-timeout threshold
    assert calls[4].args[1]["threshold_min"] == settings.awaiting_confirmation_stale_minutes
    # Research sessions (call 6) — base threshold
    assert calls[5].args[1]["threshold_min"] == settings.stale_threshold_minutes
    # Paused research (call 7) — no threshold_min param (expires_at driven)
    assert "threshold_min" not in calls[6].args[1]
    # Assist abandoned (call 8) — days-based threshold, distinct param name
    assert "threshold_min" not in calls[7].args[1]
    assert calls[7].args[1]["threshold_days"] == settings.assist_idle_threshold_days


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


# ---------------------------------------------------------------------------
# §17.422 — planning-reaper shadow regression guard
# ---------------------------------------------------------------------------

def test_long_phase_reaper_excludes_planning():
    """'planning' must NOT be in the long-phase IN-list. _REAP_LONG_PHASE runs
    before _REAP_PLANNING in the same transaction, so including 'planning'
    there would set the job 'failed' before the dedicated planning reaper
    could set it 'cancelled' — the shadow §17.422 fixes (dead under default
    45<60 and live 1440==1440 configs)."""
    assert "'planning'" not in cleanup._REAP_LONG_PHASE_SQL
    assert "'researching'" in cleanup._REAP_LONG_PHASE_SQL
    assert "'refining'" in cleanup._REAP_LONG_PHASE_SQL
    # The dedicated planning reaper is the sole handler and ends 'cancelled'.
    assert "status = 'planning'" in cleanup._REAP_PLANNING_SQL
    assert "'cancelled'" in cleanup._REAP_PLANNING_SQL
