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
    # §17.418 — prune orphan jobstore entries after start() (jobstore is now
    # live and get_jobs is authoritative). Heals orphans from any cause:
    # add-then-commit-failure, crash between the two transactions, etc.
    await _reconcile_orphans()
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
    #
    # §17.418 — this reaches into APScheduler internals (``_executors`` /
    # ``AsyncIOExecutor._pending_futures``), valid for the pinned
    # ``apscheduler==3.10.4``. The ``getattr(..., default)`` guards keep this
    # from crashing if a future bump renames them — but it would then SILENTLY
    # degrade the drain to a no-op (the §17.137 bug returns: jobs cancelled
    # abruptly, sessions stranded ``running``). ``tests/test_scheduler_shutdown.py``
    # asserts a real ``AsyncIOExecutor`` still exposes ``_pending_futures`` so a
    # bump that breaks this fails loudly. If that test fails after upgrading,
    # re-derive the drain against the new internals before shipping.
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
            "SELECT id, topic, depth, cron_expression, timezone, domain "
            "FROM scheduled_jobs WHERE enabled = TRUE"
        ))).mappings().all()
    ok = skipped = 0
    for r in rows:
        try:
            _add_job(r["id"], r["topic"], r["depth"], r["cron_expression"],
                     r["timezone"], r["domain"])
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


async def _reconcile_orphans() -> None:
    """§17.418 — prune APScheduler jobs with no matching *enabled*
    ``scheduled_jobs`` row.

    The ``SQLAlchemyJobStore`` commits add/remove on its OWN engine,
    independent of the request's asyncpg transaction. So a request that
    registers a job (``add_schedule``) then fails to commit its
    ``scheduled_jobs`` row — e.g. a pool-exhaustion commit failure, the very
    condition scheduled jobs can provoke (``config.py``) — leaves an orphan
    APScheduler job that fires ``_execute_research_job`` every tick, doing
    real research and recording nothing (its result-write hits
    ``rowcount=0``). ``_rehydrate`` only ADDS, so without this pass orphans
    survive every restart.

    Runs after ``start()`` so the jobstore is live and ``get_jobs`` is
    authoritative. Best-effort: a failure here is logged, never fatal to
    startup. Only the default (SQLAlchemy) jobstore is scanned — the X.26
    observability jobs live in the in-memory store and carry non-``schedule_``
    ids, so they're doubly excluded.
    """
    if _scheduler is None:
        return
    try:
        async with async_session() as db:
            ids = (await db.execute(text(
                "SELECT id FROM scheduled_jobs WHERE enabled = TRUE"
            ))).scalars().all()
        valid = {f"schedule_{i}" for i in ids}
        removed = 0
        for job in _scheduler.get_jobs(jobstore="default"):
            jid = getattr(job, "id", "")
            if jid.startswith("schedule_") and jid not in valid:
                try:
                    _scheduler.remove_job(jid)
                    removed += 1
                    logger.warning(
                        'event="scheduler_orphan_pruned" job_id=%s', jid,
                    )
                except Exception as exc:
                    logger.error(
                        'event="scheduler_orphan_prune_failed" '
                        'job_id=%s error=%s', jid, exc,
                    )
        if removed:
            logger.warning(
                'event="scheduler_reconcile_summary" pruned=%d', removed,
            )
    except Exception as exc:
        logger.error('event="scheduler_reconcile_failed" error=%s', exc)


def _add_job(
    schedule_id: int,
    topic: str,
    depth: str,
    cron_expr: str,
    tz: str,
    domain: Optional[str] = None,
) -> None:
    """Register an APScheduler job. Timezone threads through per-schedule (#8).

    §17.797 — ``domain`` is passed as a job arg so the recurring run can pin its
    ingest partition (``None`` = auto-detect, the pre-§17.797 behavior). Kept
    last with a default so APScheduler jobs persisted before this change (3-arg
    ``args``) still bind to ``_execute_research_job`` on rehydrate.
    """
    if _scheduler is None:
        raise RuntimeError("Scheduler not initialized; cannot add job")
    _scheduler.add_job(
        _execute_research_job,
        trigger=CronTrigger.from_crontab(cron_expr, timezone=tz),
        id=f"schedule_{schedule_id}",
        args=[schedule_id, topic, depth, domain],
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
    domain: Optional[str] = None,
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

    _add_job(schedule_id, topic, depth, cron_expr, tz, domain)
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
        "SELECT topic, depth, cron_expression, timezone, domain "
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
                    row["cron_expression"], row["timezone"], row["domain"],
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


def _log_model_ab_recommendation(task_name: str, models: list[str], summary: dict) -> None:
    """§17.578 — emit a recommendation when a candidate beats the incumbent
    (models[0]) clean: equal-or-better pass rate, zero errors, and faster."""
    def _rate(m: str) -> float:
        s = summary.get(m, {})
        scored = s.get("trials", 0) - s.get("errors", 0)
        return (s.get("passed", 0) / scored) if scored else 0.0

    def _wall(m: str) -> float:
        ws = summary.get(m, {}).get("wall_s", [])
        return (sum(ws) / len(ws)) if ws else 0.0

    incumbent = models[0]
    inc_rate, inc_wall = _rate(incumbent), _wall(incumbent)
    best = max(models, key=lambda m: (_rate(m), -_wall(m)))
    if (best != incumbent and summary.get(best, {}).get("errors", 0) == 0
            and _rate(best) >= inc_rate and _wall(best) < inc_wall and inc_wall > 0):
        logger.warning(
            'event="model_ab_recommend" task=%s incumbent=%s candidate=%s '
            'incumbent_rate=%.2f candidate_rate=%.2f speedup=%.2fx',
            task_name, incumbent, best, inc_rate, _rate(best),
            inc_wall / _wall(best) if _wall(best) else 0.0,
        )
    else:
        logger.info(
            'event="model_ab_no_change" task=%s incumbent=%s rate=%.2f',
            task_name, incumbent, inc_rate,
        )


async def _execute_model_ab_job(schedule_id: int, topic: str, depth: str) -> None:
    """§17.578 — scheduled re-A/B governance job. ``topic='model_ab:<task>'``,
    ``depth`` = comma-separated model list (first = incumbent). Re-runs the A/B
    harness and logs a recommendation when a candidate wins clean. Fail-soft;
    updates scheduled_jobs like the research path."""
    started = datetime.now(timezone.utc)
    status = "success"
    try:
        task_name = topic.split(":", 1)[1].strip() or "codegen"
        models = [m.strip() for m in (depth or "").split(",") if m.strip()]
        if len(models) < 2:
            logger.warning(
                'event="model_ab_schedule_skipped" reason="need 2+ models" schedule_id=%s',
                schedule_id)
            status = "failed"
        else:
            from app.utils.http_clients import init_clients
            init_clients()
            from scripts.model_ab import run_model_ab_task
            result = await asyncio.wait_for(
                run_model_ab_task(task_name, models, repeat=3),
                timeout=settings.scheduler_job_timeout,
            )
            _log_model_ab_recommendation(task_name, models, result["summary"])
    except asyncio.TimeoutError:
        status = "timeout"
        logger.error('event="model_ab_timeout" schedule_id=%s', schedule_id)
    except asyncio.CancelledError:
        # §17.602 — scheduler-drain (§17.137) cancels in-flight jobs on shutdown.
        # CancelledError is a BaseException, so the `except Exception` below never
        # caught it and the scheduled_jobs result-write in the finally was
        # dropped. Mark + re-raise; the finally does the write under
        # asyncio.shield (mirrors §17.155 in _execute_research_job).
        status = "cancelled"
        logger.warning('event="model_ab_drain_cancelled" schedule_id=%s', schedule_id)
        raise
    except Exception as exc:  # noqa: BLE001 — fail-soft governance job
        status = "failed"
        logger.exception('event="model_ab_failed" schedule_id=%s err=%s', schedule_id, exc)
    finally:
        next_run: Optional[datetime] = None
        if _scheduler is not None:
            job = _scheduler.get_job(f"schedule_{schedule_id}")
            if job is not None:
                next_run = job.next_run_time

        async def _write_result() -> None:
            async with async_session() as db:
                result = await db.execute(text("""
                    UPDATE scheduled_jobs
                    SET last_run_at = :ts, last_status = :st,
                        run_count = run_count + 1,
                        failure_count = failure_count + CASE WHEN :st IN ('failed','timeout') THEN 1 ELSE 0 END,
                        next_run_at = :nr, updated_at = NOW()
                    WHERE id = :id
                """), {"ts": started, "st": status, "nr": next_run, "id": schedule_id})
                await db.commit()
                # §17.613 (audit #30) — mirror the research path's audit line so a
                # model_ab schedule deleted mid-run leaves a trace, not a silent no-op.
                if result.rowcount == 0:
                    logger.warning(
                        'event="model_ab_result_write_skipped" '
                        'schedule_id=%s reason="row_missing"',
                        schedule_id,
                    )

        try:
            # §17.602 — shield so the result-write commits even when this
            # coroutine is being drain-cancelled (the except above re-raised).
            await asyncio.shield(_write_result())
        except asyncio.CancelledError:
            logger.warning(
                'event="model_ab_result_write_shielded" schedule_id=%s '
                '— UPDATE continues on loop', schedule_id)
            raise
        except Exception:
            logger.exception('event="model_ab_result_write_failed" schedule_id=%s', schedule_id)


async def _execute_research_job(
    schedule_id: int, topic: str, depth: str, domain: Optional[str] = None,
) -> None:
    """APScheduler entrypoint. Runs research with timeout, captures real session_id (#79),
    converts epoch next_run_time → TIMESTAMPTZ correctly (#7), enforces timeout (#80).

    §17.578 — a ``model_ab:<task>`` topic routes to the scheduled re-A/B job instead.
    §17.797 — ``domain`` pins the ingest partition (``None`` = auto-detect). Defaulted
    so APScheduler jobs persisted before this change (3-arg ``args``) still bind."""
    if topic.startswith("model_ab:"):
        await _execute_model_ab_job(schedule_id, topic, depth)
        return
    from app.modules.research_agent import run_research

    started = datetime.now(timezone.utc)
    session_id: Optional[str] = None
    status = "success"

    async def _consume() -> None:
        nonlocal session_id
        async for event in run_research(topic=topic, depth=depth, domain=domain):
            # run_research yields SSE-formatted strings or dicts; capture session_id
            # from the first event that carries it. Keep logic defensive — format may vary.
            if session_id is None:
                sid = _extract_session_id(event)
                if sid:
                    session_id = sid

    timed_out = False
    drain_cancelled = False
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
        except asyncio.CancelledError:
            # §17.155 follow-up — scheduler-drain (§17.137) cancels in-flight
            # jobs when shutdown_scheduler's drain timeout expires. CancelledError
            # is a BaseException, NOT Exception, so the prior ``except Exception``
            # below did not catch it. Without finalization here, the session row
            # stayed ``status='running'`` until the §17.85 reaper cleaned it up
            # ~30 min later. Mark + re-raise so asyncio semantics are preserved;
            # the finally block does the DB write under asyncio.shield.
            drain_cancelled = True
            status = "cancelled"
            logger.warning(
                'event="scheduled_research_drain_cancelled" schedule_id=%s session_id=%s',
                schedule_id, session_id,
            )
            raise
        except Exception as exc:
            status = "failed"
            logger.error(
                'event="scheduled_research_failed" schedule_id=%s session_id=%s error=%s',
                schedule_id, session_id, exc,
            )
    finally:
        # On timeout or scheduler-drain cancel, run_research was cancelled
        # mid-stream — its session row stays 'running' and waits 30 min for
        # the reaper. Finalize it here so downstream consumers see the
        # terminal status immediately.
        if (timed_out or drain_cancelled) and session_id:
            cancel_status = "cancelled"
            cancel_msg = (
                "scheduler_drain_cancelled" if drain_cancelled
                else "Scheduled run exceeded scheduler_job_timeout"
            )

            async def _finalize_cancel() -> None:
                async with async_session() as db:
                    result = await db.execute(text("""
                        UPDATE research_sessions
                        SET status = :st,
                            error_message = COALESCE(error_message, :msg),
                            updated_at = NOW(),
                            completed_at = NOW()
                        WHERE id = :sid
                          AND status IN ('pending', 'running')
                    """), {"sid": session_id, "st": cancel_status, "msg": cancel_msg})
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

            try:
                # §17.155 follow-up — wrap in asyncio.shield so the DB UPDATE
                # commits even when this coroutine is itself being cancelled
                # (the drain path re-raises CancelledError above). Mirrors the
                # §17.168 pattern in research_state._run_with_session_lifecycle.
                await asyncio.shield(_finalize_cancel())
            except asyncio.CancelledError:
                # Caller-side cancellation hit while waiting on the shielded
                # finalize. The DB UPDATE is still in flight on the event loop
                # and will commit. Re-raise so cancellation propagates correctly.
                logger.warning(
                    'event="scheduled_research_cancel_shielded" '
                    'schedule_id=%s session_id=%s '
                    '— UPDATE continues on loop',
                    schedule_id, session_id,
                )
                raise
            except Exception as exc:
                logger.error(
                    'event="scheduled_research_cancel_write_failed" '
                    'schedule_id=%s session_id=%s error=%s',
                    schedule_id, session_id, exc,
                )

    # NOTE: a drain-cancel raised in the inner try re-raises out of this
    # function after the finally above finalizes the session row; we never
    # reach the scheduled_jobs result-write below on that path.

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
