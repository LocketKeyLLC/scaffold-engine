"""APScheduler integration for /research recurrence.

Rehydrates scheduled_jobs from Postgres on startup. Jobs execute
run_research() directly in-process via asyncio, with a timeout wrapper
and capture of the real research_sessions.id into scheduled_jobs.last_job_id.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

from app.config import settings
from app.database import async_session

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> Optional[AsyncIOScheduler]:
    """Return the running scheduler, or None if disabled/not started.

    Callers must handle the None case — scheduler_enabled=False is legal.
    """
    return _scheduler


async def init_scheduler() -> Optional[AsyncIOScheduler]:
    """Create the scheduler, rehydrate from DB, start it.

    Idempotent: a prior scheduler (if any) is shut down before re-init.
    Returns None when settings.scheduler_enabled is False.
    """
    global _scheduler

    # Idempotency: tear down any prior instance cleanly.
    if _scheduler is not None:
        logger.info('event="scheduler_reinit" shutting_down_prior=true')
        await shutdown_scheduler()

    if not settings.scheduler_enabled:
        logger.info('event="scheduler_disabled"')
        return None

    jobstore_url = settings.scheduler_jobstore_url or settings.sync_database_url
    jobstore = SQLAlchemyJobStore(url=jobstore_url, tablename="apscheduler_jobs")
    _scheduler = AsyncIOScheduler(
        jobstores={"default": jobstore},
        timezone=settings.scheduler_timezone,
    )
    await _rehydrate()
    _register_observability_jobs()
    _scheduler.start()
    logger.info('event="scheduler_started" jobs=%d', len(_scheduler.get_jobs()))
    return _scheduler


def _register_observability_jobs() -> None:
    """Sprint X.26 — register the X.20 push eval + calibration watchdog
    interval jobs alongside the cron-driven research schedules.

    These jobs use APScheduler's in-memory store via ``jobstore='memory'``
    so they don't pollute the ``apscheduler_jobs`` SQLAlchemy table that
    rehydrates user-defined research schedules. ``replace_existing=True``
    keeps re-init idempotent.
    """
    if _scheduler is None:
        return

    # in-memory jobstore registered lazily so the SQLAlchemy store stays
    # the default for user schedules.
    try:
        _scheduler.add_jobstore("memory", alias="memory")
    except Exception:
        # already registered (re-init path) — ignore.
        pass

    if settings.alert_eval_enabled:
        from app.observability import thresholds as _thresholds
        _scheduler.add_job(
            _thresholds.tick,
            trigger=IntervalTrigger(seconds=settings.alert_eval_interval_seconds),
            id="x26_threshold_eval",
            jobstore="memory",
            replace_existing=True,
            misfire_grace_time=settings.scheduler_misfire_grace_time,
        )
        logger.info(
            'event="threshold_eval_registered" interval_s=%d window_m=%d',
            settings.alert_eval_interval_seconds,
            settings.alert_eval_window_minutes,
        )

    if settings.calibration_watchdog_enabled:
        from app.observability import calibration_watchdog as _watchdog
        _scheduler.add_job(
            _watchdog.tick,
            trigger=IntervalTrigger(
                seconds=settings.calibration_watchdog_interval_seconds,
            ),
            id="x26_calibration_watchdog",
            jobstore="memory",
            replace_existing=True,
            misfire_grace_time=settings.scheduler_misfire_grace_time,
        )
        logger.info(
            'event="calibration_watchdog_registered" interval_s=%d grace_m=%d',
            settings.calibration_watchdog_interval_seconds,
            settings.calibration_grace_minutes,
        )


async def shutdown_scheduler() -> None:
    """Graceful shutdown with an explicit async drain of in-flight jobs.

    §17.137 — APScheduler 3.10's ``AsyncIOExecutor.shutdown(wait=True)``
    is documented as not honoring wait:

        # There is no way to honor wait=True without converting this
        # method into a coroutine method
        for f in self._pending_futures:
            if not f.done():
                f.cancel()

    So calling ``sched.shutdown(wait=True)`` would CANCEL every
    ``_execute_research_job`` mid-flight, leaving its ``research_sessions``
    row stranded in ``running``. We bypass that by collecting each
    executor's pending futures ourselves and awaiting them with
    ``asyncio.wait_for`` (bounded by ``settings.scheduler_shutdown_timeout``).
    Only after the drain completes — or its timeout fires + we cancel
    explicitly — do we call ``sched.shutdown(wait=False)`` to tear down
    the scheduler bookkeeping.

    The singleton guard is flipped to None FIRST so a re-entrant caller
    (e.g. a second SIGTERM) sees a no-op rather than racing the drain.
    """
    global _scheduler
    if _scheduler is None:
        return

    sched = _scheduler
    _scheduler = None  # flip the guard first so re-entrant callers see None

    # Pause so no NEW jobs fire while we drain — the cron tick may still
    # try to dispatch during shutdown without this.
    try:
        sched.pause()
    except Exception as exc:
        logger.debug("scheduler_pause_failed: err=%s", exc)

    # Snapshot pending async futures across every executor. We list()
    # the set because the executor's done-callbacks mutate it as tasks
    # complete during our drain.
    pending: list = []
    for executor in getattr(sched, "_executors", {}).values():
        futs = getattr(executor, "_pending_futures", None)
        if not futs:
            continue
        pending.extend(f for f in list(futs) if not f.done())

    drained_graceful = True
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=settings.scheduler_shutdown_timeout,
            )
            logger.info(
                'event="scheduler_drained" pending=%d', len(pending),
            )
        except asyncio.TimeoutError:
            drained_graceful = False
            logger.warning(
                'event="scheduler_drain_timeout" pending=%d timeout=%ds — cancelling',
                len(pending), settings.scheduler_shutdown_timeout,
            )
            for f in pending:
                if not f.done():
                    f.cancel()
            # Give cancelled tasks a brief moment to run their finally blocks.
            # 2 s is enough for any reasonable cleanup; we don't want to
            # extend the total shutdown budget materially past the
            # configured timeout.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    'event="scheduler_drain_cancel_unfinished" pending=%d',
                    sum(1 for f in pending if not f.done()),
                )

    # APScheduler bookkeeping. wait=False is now safe because we already
    # drained (or cancelled) the asyncio tasks ourselves; AsyncIOExecutor's
    # shutdown is a no-op since _pending_futures is empty.
    try:
        sched.shutdown(wait=False)
    except Exception as exc:
        logger.debug("scheduler_shutdown_failed: err=%s", exc)

    logger.info(
        'event="scheduler_stopped" graceful=%s pending=%d',
        "true" if drained_graceful else "false",
        len(pending),
    )


async def _rehydrate() -> None:
    """Re-add enabled schedules from DB on startup (source of truth = scheduled_jobs).

    Per-row try/except: a single bad cron expression (or other row-level defect)
    is logged and skipped. Other schedules still register.
    """
    async with async_session() as db:
        rows = (await db.execute(text(
            "SELECT id, topic, depth, cron_expression, timezone "
            "FROM scheduled_jobs WHERE enabled = TRUE"
        ))).mappings().all()
    ok = skipped = 0
    for r in rows:
        try:
            _add_job(r["id"], r["topic"], r["depth"], r["cron_expression"], r["timezone"])
            ok += 1
        except Exception as exc:
            skipped += 1
            logger.error(
                'event="schedule_rehydrate_skipped" schedule_id=%s cron=%r tz=%r error=%s',
                r.get("id"), r.get("cron_expression"), r.get("timezone"), exc,
            )
    if skipped:
        logger.warning(
            'event="schedule_rehydrate_summary" ok=%d skipped=%d', ok, skipped,
        )


def _add_job(
    schedule_id: int,
    topic: str,
    depth: str,
    cron_expr: str,
    tz: str,
) -> None:
    """Register an APScheduler job. Timezone threads through per-schedule (#8)."""
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized; cannot add job")
    _scheduler.add_job(
        _execute_research_job,
        trigger=CronTrigger.from_crontab(cron_expr, timezone=tz),
        id=f"schedule_{schedule_id}",
        args=[schedule_id, topic, depth],
        replace_existing=True,
        misfire_grace_time=settings.scheduler_misfire_grace_time,
    )


async def add_schedule(
    db,
    schedule_id: int,
    topic: str,
    depth: str,
    cron_expr: str,
    tz: str = "UTC",
) -> Optional[datetime]:
    """Register a new schedule and write next_run_at in the caller's session.

    Symmetric register-with-rollback: the APScheduler entry is created
    first, then the next_run_at UPDATE runs in ``db`` (caller's session).
    The caller is responsible for committing. If anything in this function
    raises after APScheduler registration, the in-memory job is unregistered
    so that the caller's subsequent rollback leaves the system aligned.

    Returns the computed next_run_time so the caller can populate its
    response payload without a re-read.
    """
    if _scheduler is None:
        logger.warning('event="add_schedule_skipped" reason="scheduler_disabled"')
        return None

    _add_job(schedule_id, topic, depth, cron_expr, tz)
    try:
        job = _scheduler.get_job(f"schedule_{schedule_id}")
        next_run = job.next_run_time if job else None  # tz-aware datetime → TIMESTAMPTZ
        await db.execute(text(
            "UPDATE scheduled_jobs "
            "SET next_run_at = :nr, updated_at = NOW() "
            "WHERE id = :id"
        ), {"nr": next_run, "id": schedule_id})
        return next_run
    except Exception:
        # Caller will roll back ``db``; unregister the APScheduler entry so
        # the runtime state matches the caller's view post-rollback.
        try:
            _scheduler.remove_job(f"schedule_{schedule_id}")
        except Exception as cleanup_exc:
            logger.warning(
                'event="add_schedule_rollback_cleanup_failed" '
                'schedule_id=%s error=%s',
                schedule_id, cleanup_exc,
            )
        raise


async def delete_schedule(db, schedule_id: int) -> bool:
    """Unregister + delete a schedule in the caller's session.

    Symmetric to ``add_schedule``: APScheduler is unregistered first, then
    the DB row is deleted in ``db``. If the DB delete raises, the schedule
    is re-registered from the row we read up-front so APScheduler stays in
    sync with the caller's eventual rollback.

    Returns False when no row exists for ``schedule_id`` (caller should 404
    without committing). Returns True after a successful delete; the caller
    is responsible for committing ``db``.
    """
    result = await db.execute(text(
        "SELECT topic, depth, cron_expression, timezone "
        "FROM scheduled_jobs WHERE id = :id"
    ), {"id": schedule_id})
    row = result.mappings().first()
    if not row:
        return False

    if _scheduler is not None:
        job_id = f"schedule_{schedule_id}"
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)

    try:
        await db.execute(
            text("DELETE FROM scheduled_jobs WHERE id = :id"),
            {"id": schedule_id},
        )
    except Exception:
        # Re-register so APScheduler stays aligned with the DB row that
        # the caller's rollback will preserve.
        if _scheduler is not None:
            try:
                _add_job(
                    schedule_id, row["topic"], row["depth"],
                    row["cron_expression"], row["timezone"],
                )
            except Exception as readd_exc:
                logger.error(
                    'event="delete_schedule_re_add_failed" '
                    'schedule_id=%s error=%s',
                    schedule_id, readd_exc,
                )
        raise
    return True


async def remove_schedule(schedule_id: int) -> None:
    """APScheduler-only unregister (no DB write).

    Retained for callers that need to detach a job from APScheduler without
    touching ``scheduled_jobs`` (e.g. test fixtures). End-user delete flow
    should use :func:`delete_schedule` for symmetric DB+APScheduler
    semantics.

    Distinguishes the two no-op cases at the log layer so failures are
    easier to diagnose: "scheduler not initialized" vs "job not currently
    registered".
    """
    if _scheduler is None:
        logger.debug(
            'event="remove_schedule_noop" reason="scheduler_not_initialized" '
            'schedule_id=%s', schedule_id,
        )
        return
    job_id = f"schedule_{schedule_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    else:
        logger.debug(
            'event="remove_schedule_noop" reason="job_not_registered" '
            'schedule_id=%s', schedule_id,
        )


async def _execute_research_job(schedule_id: int, topic: str, depth: str) -> None:
    """APScheduler entrypoint. Runs research with timeout, captures real session_id (#79),
    converts epoch next_run_time → TIMESTAMPTZ correctly (#7), enforces timeout (#80)."""
    from app.modules.research_agent import run_research

    started = datetime.now(timezone.utc)
    session_id: Optional[str] = None
    status = "success"

    async def _consume() -> None:
        nonlocal session_id
        async for event in run_research(topic=topic, depth=depth, domain=None):
            # run_research yields SSE-formatted strings or dicts; capture session_id
            # from the first event that carries it. Keep logic defensive — format may vary.
            if session_id is None:
                sid = _extract_session_id(event)
                if sid:
                    session_id = sid

    timed_out = False
    try:
        try:
            await asyncio.wait_for(_consume(), timeout=settings.scheduler_job_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            status = "timeout"
            logger.error(
                'event="scheduled_research_timeout" schedule_id=%s session_id=%s timeout=%ds',
                schedule_id, session_id, settings.scheduler_job_timeout,
            )
        except Exception as exc:
            status = "failed"
            logger.error(
                'event="scheduled_research_failed" schedule_id=%s session_id=%s error=%s',
                schedule_id, session_id, exc,
            )
    finally:
        # On timeout, run_research was cancelled mid-stream — its session row
        # stays 'running' and waits 30 min for the reaper. Finalize it here so
        # downstream consumers see 'cancelled' immediately.
        if timed_out and session_id:
            try:
                async with async_session() as db:
                    result = await db.execute(text("""
                        UPDATE research_sessions
                        SET status = 'cancelled',
                            error_message = COALESCE(error_message,
                                'Scheduled run exceeded scheduler_job_timeout'),
                            updated_at = NOW(),
                            completed_at = NOW()
                        WHERE id = :sid
                          AND status IN ('pending', 'running')
                    """), {"sid": session_id})
                    await db.commit()
                    # Zero rows = the session moved off pending/running before
                    # we got here (user /cancel, reaper, or successful resume
                    # racing with the timeout). Log so silent state-machine
                    # divergences are visible in audit, not just gone.
                    if result.rowcount == 0:
                        logger.warning(
                            'event="scheduled_research_cancel_no_op" '
                            'schedule_id=%s session_id=%s '
                            'reason="session_already_terminal_or_concurrent_update"',
                            schedule_id, session_id,
                        )
            except Exception as exc:
                logger.error(
                    'event="scheduled_research_cancel_write_failed" '
                    'schedule_id=%s session_id=%s error=%s',
                    schedule_id, session_id, exc,
                )

    # Compute next_run_at from the live scheduler job (avoids the DOUBLE-PRECISION
    # → TIMESTAMPTZ type mismatch that the old subquery had).
    next_run: Optional[datetime] = None
    if _scheduler is not None:
        job = _scheduler.get_job(f"schedule_{schedule_id}")
        if job is not None:
            next_run = job.next_run_time  # already a tz-aware datetime

    async with async_session() as db:
        result = await db.execute(text("""
            UPDATE scheduled_jobs
            SET last_run_at = :ts,
                last_status = :st,
                last_job_id = :jid,
                run_count = run_count + 1,
                failure_count = failure_count + CASE WHEN :st IN ('failed', 'timeout') THEN 1 ELSE 0 END,
                next_run_at = :nr,
                updated_at = NOW()
            WHERE id = :id
        """), {
            "ts": started,
            "st": status,
            "jid": session_id,
            "nr": next_run,
            "id": schedule_id,
        })
        await db.commit()
        if result.rowcount == 0:
            logger.warning(
                'event="scheduled_research_result_write_skipped" '
                'schedule_id=%s session_id=%s reason="row_missing"',
                schedule_id, session_id,
            )

    if status == "success":
        logger.info(
            'event="scheduled_research_completed" schedule_id=%s session_id=%s '
            'duration_s=%.1f',
            schedule_id, session_id,
            (datetime.now(timezone.utc) - started).total_seconds(),
        )


def _extract_session_id(event) -> Optional[str]:
    """Best-effort pull of session_id from a research_agent SSE event.

    research_agent yields via _sse(event_type, payload) which produces strings
    like 'event: X\\ndata: {...}\\n\\n'. We parse the data line if present.
    Also tolerates raw dicts in case the generator shape changes.
    """
    if isinstance(event, dict):
        return event.get("session_id")
    if not isinstance(event, str):
        return None
    # Cheap parse — find data:{...} with session_id
    if '"session_id"' not in event:
        return None
    try:
        for line in event.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                sid = payload.get("session_id")
                if sid:
                    return str(sid)
    except Exception:
        return None
    return None
