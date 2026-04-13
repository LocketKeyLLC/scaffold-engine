"""
cleanup.py — Periodic stale-job reaper.

Runs every 15 minutes, marks stuck jobs as failed.
Uses a strong-reference task registry to prevent GC collection.
"""
import asyncio
import logging
from typing import Final, Set

from sqlalchemy import text

from app.database import get_db

from app.utils.staleness import sweep_expired

logger = logging.getLogger(__name__)

_background_tasks: Set[asyncio.Task] = set()

CLEANUP_INTERVAL_SECONDS: Final[int] = 900
STALE_THRESHOLD_MINUTES: Final[int] = 30

_REAP_SQL: Final[str] = """
    UPDATE jobs
    SET status = 'failed',
        compiled_output = 'Reaped by scheduled cleanup: exceeded stale threshold',
        updated_at = NOW()
    WHERE status IN ('running', 'executing')
      AND updated_at < NOW() - INTERVAL '30 minutes'
      AND NOT EXISTS (
          SELECT 1 FROM dag_nodes
          WHERE dag_nodes.job_id = jobs.id
            AND dag_nodes.status = 'running'
      )
"""


async def _reap_stale_jobs() -> int:
    """Mark stale running/executing jobs as failed. Returns reaped count."""
    async for db in get_db():
        result = await db.execute(text(_REAP_SQL))
        await db.commit()
        count: int = result.rowcount
        if count:
            logger.info("stale_job_cleaned count=%d", count)
        return count
    return 0


async def _cleanup_loop() -> None:
    """Infinite loop — reap stale jobs every CLEANUP_INTERVAL_SECONDS."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            await _reap_stale_jobs()
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
