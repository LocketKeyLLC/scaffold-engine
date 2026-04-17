"""APScheduler integration for /research recurrence.

Rehydrates scheduled_jobs from Postgres on startup. Jobs execute
run_research() directly in-process via asyncio.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.config import settings
from app.database import async_session

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized; call init_scheduler() first")
    return _scheduler


async def init_scheduler() -> AsyncIOScheduler:
    """Create the scheduler, rehydrate from DB, start it."""
    global _scheduler
    if not settings.scheduler_enabled:
        logger.info('event="scheduler_disabled"')
        return None
    jobstore_url = settings.scheduler_jobstore_url or settings.sync_database_url
    jobstore = SQLAlchemyJobStore(url=jobstore_url, tablename="apscheduler_jobs")
    _scheduler = AsyncIOScheduler(jobstores={"default": jobstore}, timezone=settings.scheduler_timezone)
    await _rehydrate()
    _scheduler.start()
    logger.info('event="scheduler_started" jobs=%d', len(_scheduler.get_jobs()))
    return _scheduler


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info('event="scheduler_stopped"')


async def _rehydrate() -> None:
    """Re-add enabled schedules from DB on startup (source of truth = scheduled_jobs)."""
    async with async_session() as db:
        rows = (await db.execute(text(
            "SELECT id, topic, depth, cron_expression FROM scheduled_jobs WHERE enabled = TRUE"
        ))).mappings().all()
    for r in rows:
        _add_job(r["id"], r["topic"], r["depth"], r["cron_expression"])


def _add_job(schedule_id: int, topic: str, depth: str, cron_expr: str) -> None:
    get_scheduler().add_job(
        _execute_research_job,
        trigger=CronTrigger.from_crontab(cron_expr, timezone="UTC"),
        id=f"schedule_{schedule_id}",
        args=[schedule_id, topic, depth],
        replace_existing=True,
        misfire_grace_time=300,
    )


async def add_schedule(schedule_id: int, topic: str, depth: str, cron_expr: str) -> None:
    _add_job(schedule_id, topic, depth, cron_expr)
    async with async_session() as db:
        job = get_scheduler().get_job(f"schedule_{schedule_id}")
        next_run = job.next_run_time if job else None
        await db.execute(text(
            "UPDATE scheduled_jobs SET next_run_at = :nr, updated_at = NOW() WHERE id = :id"
        ), {"nr": next_run, "id": schedule_id})
        await db.commit()


async def remove_schedule(schedule_id: int) -> None:
    job_id = f"schedule_{schedule_id}"
    if get_scheduler().get_job(job_id):
        get_scheduler().remove_job(job_id)


async def _execute_research_job(schedule_id: int, topic: str, depth: str) -> None:
    """APScheduler entrypoint. Calls run_research() and updates scheduled_jobs."""
    from app.modules.research_agent import run_research
    from uuid import uuid4

    research_job_id = str(uuid4())
    started = datetime.now(timezone.utc)
    status = "success"
    try:
        async for _ in run_research(topic=topic, depth=depth, domain=None):
            pass
    except Exception as exc:
        status = "failed"
        logger.error('event="scheduled_research_failed" schedule_id=%s error=%s', schedule_id, exc)
    finally:
        async with async_session() as db:
            await db.execute(text("""
                UPDATE scheduled_jobs
                SET last_run_at = :ts, last_status = :st, last_job_id = :jid,
                    run_count = run_count + 1,
                    failure_count = failure_count + CASE WHEN :st = 'failed' THEN 1 ELSE 0 END,
                    next_run_at = (SELECT next_run_time FROM apscheduler_jobs WHERE id = :asid),
                    updated_at = NOW()
                WHERE id = :id
            """), {
                "ts": started, "st": status, "jid": research_job_id,
                "asid": f"schedule_{schedule_id}", "id": schedule_id,
            })
            await db.commit()
