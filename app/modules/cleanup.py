"""
cleanup.py — Periodic stale-job reaper.

Runs every `settings.cleanup_interval_seconds`, marks stuck jobs as failed.
State-aware thresholds:
  - executing / running                       -> settings.stale_threshold_minutes (default 30)
  - researching / refining / planning (jobs)  -> settings.long_phase_stale_minutes (default 45)
  - planning (legacy column sweep)            -> settings.planning_stale_minutes (default 60)
  - research_sessions pending/running         -> settings.stale_threshold_minutes
  - research_sessions paused past expiry      -> immediate
One eager sweep runs at task start before entering the sleep loop.
"""
import asyncio
import logging
from typing import Set

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.utils.staleness import sweep_expired

logger = logging.getLogger(__name__)

_background_tasks: Set[asyncio.Task] = set()


_REAP_ORPHAN_NODES_SQL = """
    UPDATE dag_nodes
    SET status = 'pending',
        updated_at = NOW()
    WHERE status = 'running'
      AND started_at IS NOT NULL
      AND started_at < NOW() - make_interval(mins => :threshold_min)
    RETURNING id, job_id, node_key
"""

_REFRESH_PARENT_JOBS_SQL = """
    UPDATE jobs
    SET updated_at = NOW()
    WHERE id = ANY(CAST(:job_ids AS uuid[]))
      AND status IN ('running', 'executing')
    RETURNING id
"""

_REAP_RUNNING_SQL = """
    UPDATE jobs
    SET status = 'failed',
        error_summary = :msg,
        updated_at = NOW()
    WHERE status IN ('running', 'executing')
      AND updated_at < NOW() - make_interval(mins => :threshold_min)
      AND NOT EXISTS (
          SELECT 1 FROM dag_nodes
          WHERE dag_nodes.job_id = jobs.id
            AND dag_nodes.status = 'running'
      )
    RETURNING id
"""

_REAP_LONG_PHASE_SQL = """
    UPDATE jobs
    SET status = 'failed',
        error_summary = :msg,
        updated_at = NOW()
    WHERE status IN ('researching', 'refining', 'planning')
      AND updated_at < NOW() - make_interval(mins => :threshold_min)
    RETURNING id
"""

_REAP_PLANNING_SQL = """
    UPDATE jobs
    SET status = 'cancelled',
        updated_at = NOW()
    WHERE status = 'planning'
      AND updated_at < NOW() - make_interval(mins => :threshold_min)
    RETURNING id
"""

_REAP_AWAITING_CONFIRMATION_SQL = """
    UPDATE jobs
    SET status = 'cancelled',
        error_summary = 'Awaiting confirmation gate timeout (no user reply)',
        updated_at = NOW()
    WHERE status = 'awaiting_confirmation'
      AND updated_at < NOW() - make_interval(mins => :threshold_min)
    RETURNING id
"""

_REAP_RESEARCH_SESSIONS_SQL = """
    UPDATE research_sessions
    SET status = 'failed',
        error_message = COALESCE(error_message, :msg),
        updated_at = NOW(),
        completed_at = NOW()
    WHERE status IN ('pending', 'running')
      AND updated_at < NOW() - make_interval(mins => :threshold_min)
    RETURNING id
"""

_REAP_PAUSED_RESEARCH_SQL = """
    UPDATE research_sessions
    SET status = 'cancelled',
        error_message = COALESCE(error_message, :msg),
        updated_at = NOW(),
        completed_at = NOW()
    WHERE status = 'paused_awaiting_reply'
      AND pause_expires_at IS NOT NULL
      AND pause_expires_at < NOW()
    RETURNING id
"""


async def reap_stale_jobs(db: AsyncSession) -> dict:
    """Unified stale-job reaper. Returns counts of reaped jobs.

    Replaces SQLAlchemy `rowcount` (driver-dependent for UPDATE...RETURNING on
    asyncpg) with an explicit `len(await r.fetchall())`.
    """
    base_min = settings.stale_threshold_minutes
    long_min = settings.long_phase_stale_minutes
    plan_min = settings.planning_stale_minutes
    awaiting_min = settings.awaiting_confirmation_stale_minutes
    orphan_min = settings.node_orphan_threshold_minutes

    # Stage 0 — reset orphaned dag_nodes (executor died mid-run).
    # Must run BEFORE the job reapers: _REAP_RUNNING_SQL refuses to fail a
    # job with a running node, so an orphaned node permanently locks its
    # parent. Reset → 'pending' lets /execute/all resume on next invocation.
    r0 = await db.execute(
        text(_REAP_ORPHAN_NODES_SQL),
        {"threshold_min": orphan_min},
    )
    orphan_rows = r0.fetchall()
    orphan_nodes_reset = len(orphan_rows)

    if orphan_nodes_reset:
        # Touch parent jobs so the next reap cycle doesn't immediately fail
        # the freshly-recovered job (which still sits in 'executing').
        affected_job_ids = list({str(row.job_id) for row in orphan_rows})
        await db.execute(
            text(_REFRESH_PARENT_JOBS_SQL),
            {"job_ids": affected_job_ids},
        )
        for row in orphan_rows:
            logger.warning(
                "orphan_node_reset job_id=%s node_key=%s threshold_min=%d",
                row.job_id, row.node_key, orphan_min,
            )


    r1 = await db.execute(
        text(_REAP_RUNNING_SQL),
        {
            "msg": f"Job timed out after {base_min} minutes of inactivity",
            "threshold_min": base_min,
        },
    )
    running_failed = len(r1.fetchall())

    r2 = await db.execute(
        text(_REAP_LONG_PHASE_SQL),
        {
            "msg": f"Long-phase job timed out after {long_min} minutes of inactivity",
            "threshold_min": long_min,
        },
    )
    long_phase_failed = len(r2.fetchall())

    r3 = await db.execute(
        text(_REAP_PLANNING_SQL),
        {"threshold_min": plan_min},
    )
    planning_cancelled = len(r3.fetchall())

    r3b = await db.execute(
        text(_REAP_AWAITING_CONFIRMATION_SQL),
        {"threshold_min": awaiting_min},
    )
    awaiting_cancelled = len(r3b.fetchall())
    r4 = await db.execute(
        text(_REAP_RESEARCH_SESSIONS_SQL),
        {
            "msg": f"Research session timed out after {base_min} minutes of inactivity",
            "threshold_min": base_min,
        },
    )
    research_failed = len(r4.fetchall())

    r5 = await db.execute(
        text(_REAP_PAUSED_RESEARCH_SQL),
        {"msg": "Pause expired before user reply received"},
    )
    paused_cancelled = len(r5.fetchall())

    await db.commit()

    if (orphan_nodes_reset or running_failed or long_phase_failed
            or planning_cancelled or awaiting_cancelled or research_failed or paused_cancelled):
        logger.info(
            "stale_jobs_reaped orphan_nodes_reset=%d running_to_failed=%d "
            "long_phase_to_failed=%d planning_to_cancelled=%d "
            "awaiting_to_cancelled=%d "
            "research_to_failed=%d paused_to_cancelled=%d",
            orphan_nodes_reset, running_failed, long_phase_failed,
            planning_cancelled, awaiting_cancelled, research_failed, paused_cancelled,
        )

    return {
        "orphan_nodes_reset": orphan_nodes_reset,
        "running_to_failed": running_failed,
        "long_phase_to_failed": long_phase_failed,
        "planning_to_cancelled": planning_cancelled,
        "awaiting_to_cancelled": awaiting_cancelled,
        "research_to_failed": research_failed,
        "paused_to_cancelled": paused_cancelled,
    }


async def _run_once() -> None:
    """One sweep: reap stale jobs + run TTL staleness sweep."""
    async with async_session() as db:
        await reap_stale_jobs(db)
    try:
        result = await sweep_expired()
        if result.get("expired_count", 0) > 0:
            logger.info("staleness_sweep expired=%d", result["expired_count"])
    except Exception:
        logger.debug("staleness_sweep_skipped")


async def _cleanup_loop() -> None:
    """Eager first sweep, then loop every settings.cleanup_interval_seconds."""
    try:
        await _run_once()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("cleanup_initial_sweep_error")

    while True:
        try:
            await asyncio.sleep(settings.cleanup_interval_seconds)
            await _run_once()
        except asyncio.CancelledError:
            logger.info("cleanup_loop_cancelled")
            raise
        except Exception:
            logger.exception("cleanup_loop_error")


def start_cleanup_task() -> asyncio.Task:
    """Start the cleanup loop with strong-reference protection."""
    task: asyncio.Task = asyncio.create_task(
        _cleanup_loop(), name="stale-job-cleanup"
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    logger.info(
        "cleanup_task_started interval_s=%d base_min=%d long_phase_min=%d planning_min=%d",
        settings.cleanup_interval_seconds,
        settings.stale_threshold_minutes,
        settings.long_phase_stale_minutes,
        settings.planning_stale_minutes,
    )
    return task
