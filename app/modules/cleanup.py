"""
cleanup.py — Periodic stale-job reaper.

Runs every 15 minutes, marks stuck jobs as failed.
Uses a strong-reference task registry to prevent GC collection.
"""
import asyncio
import logging
from typing import Final, Set

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.utils.staleness import sweep_expired

logger = logging.getLogger(__name__)

_background_tasks: Set[asyncio.Task] = set()

CLEANUP_INTERVAL_SECONDS: Final[int] = 900
STALE_THRESHOLD_MINUTES: Final[int] = 30

_REAP_RUNNING_SQL: Final[str] = """
    UPDATE jobs
    SET status = 'failed',
        compiled_output = :msg,
        updated_at = NOW()
    WHERE status IN ('running', 'executing')
      AND updated_at < NOW() - INTERVAL '30 minutes'
      AND NOT EXISTS (
          SELECT 1 FROM dag_nodes
          WHERE dag_nodes.job_id = jobs.id
            AND dag_nodes.status = 'running'
      )
    RETURNING id
"""

_REAP_PLANNING_SQL: Final[str] = """
    UPDATE jobs
    SET status = 'cancelled',
        updated_at = NOW()
    WHERE status = 'planning'
      AND updated_at < NOW() - INTERVAL '60 minutes'
    RETURNING id
"""

_REAP_RESEARCH_SESSIONS_SQL: Final[str] = """
    UPDATE research_sessions
    SET status = 'failed',
        error_message = COALESCE(error_message, :msg),
        updated_at = NOW(),
        completed_at = NOW()
    WHERE status IN ('pending', 'running')
      AND updated_at < NOW() - INTERVAL '30 minutes'
    RETURNING id
"""


async def reap_stale_jobs(db: AsyncSession) -> dict:
    """Unified stale-job reaper. Returns counts of reaped jobs.

    Covers:
      - running/executing > 30 min (with active-node guard) -> failed
      - planning > 60 min -> cancelled
    """
    r1 = await db.execute(
        text(_REAP_RUNNING_SQL),
        {"msg": "Job timed out after 30 minutes of inactivity"},
    )
    running_failed = r1.rowcount

    r2 = await db.execute(text(_REAP_PLANNING_SQL))
    planning_cancelled = r2.rowcount
    r3 = await db.execute(
        text(_REAP_RESEARCH_SESSIONS_SQL),
        {"msg": "Research session timed out after 30 minutes of inactivity"},
    )
    research_failed = r3.rowcount
    await db.commit()

    if running_failed or planning_cancelled or research_failed:
        logger.info(
            "stale_jobs_reaped running_to_failed=%d planning_to_cancelled=%d research_to_failed=%d",
            running_failed, planning_cancelled, research_failed,
        )

    return {
        "running_to_failed": running_failed,
        "planning_to_cancelled": planning_cancelled,
        "research_to_failed": research_failed,
    }


async def _cleanup_loop() -> None:
    """Infinite loop — reap stale jobs every CLEANUP_INTERVAL_SECONDS."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            async with async_session() as db:
                await reap_stale_jobs(db)
            try:
                result = await sweep_expired()
                if result.get("expired_count", 0) > 0:
                    logger.info("staleness_sweep expired=%d", result["expired_count"])
            except Exception:
                logger.debug("staleness_sweep_skipped")
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
    logger.info("cleanup_task_started interval_s=%d", CLEANUP_INTERVAL_SECONDS)
    return task
