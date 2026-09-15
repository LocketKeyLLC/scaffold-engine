"""§17.1076 — the Postgres task queue (procrastinate), as a trial.

The engine's background work — the stale-job reaper + staleness sweep
(`modules/cleanup.py`), the three APScheduler interval jobs, the detached
run broker's zombie sweep and the resume-on-startup loop — is a durable
job queue built piece by piece: each loop owns its own retry, its own
interval and its own "did the process die mid-task" story. procrastinate
gives one queue on the Postgres the engine already has (no broker), with
retries, locks and periodic tasks declared once.

The trial ported ONE thing — the cleanup sweep as a periodic task — behind
``queue_enabled`` (default off; the asyncio loop keeps running when off).
§17.1078 ports the rest: the three observability/learning interval jobs
(threshold eval, calibration watchdog, role→model learning) and an
AGE-based zombie turn-run sweep (the startup sweep stays — at boot every
'running' row is a corpse; between boots only an old one is).
The worker is a separate container from the engine image (compose profile
``queue``), so a task never competes with request handling. Schema: the
queue's own tables, applied by ``make queue-schema`` (idempotent) — NOT by
the migration runners, on purpose: it is procrastinate's schema, versioned
by procrastinate.
"""
from __future__ import annotations

import logging

import procrastinate

from app.config import settings

logger = logging.getLogger("scaffold.queue")


def _conninfo() -> str:
    # settings.database_url is postgresql+asyncpg://…; psycopg wants a plain DSN
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=_conninfo()))


@app.periodic(cron=settings.queue_cleanup_cron)
@app.task(name="scaffold.cleanup_sweep", queue="maintenance", retry=2, lock="cleanup_sweep")
async def cleanup_sweep(timestamp: int) -> dict:
    """The §17.422 reaper + TTL staleness sweep, as a queue task: one at a
    time (lock), two retries on failure, every run recorded in the
    procrastinate_jobs table with its outcome."""
    from app.database import async_session
    from app.modules.cleanup import reap_stale_jobs, sweep_expired
    async with async_session() as db:
        reaped = await reap_stale_jobs(db)
    try:
        swept = await sweep_expired()
    except Exception:  # noqa: BLE001 — same fail-soft as the loop
        logger.warning("staleness_sweep_failed", exc_info=True)
        swept = {"error": True}
    out = {"reaped": reaped, "swept": swept, "ts": timestamp}
    logger.info("queue_cleanup_sweep: %s", out)
    return out


def cron_from_seconds(seconds: int) -> str:
    """The interval jobs are configured in seconds; procrastinate periodics
    take cron. Minutes that divide the hour → `*/m * * * *`; whole hours
    that divide the day → `0 */h * * *`; whole days → `0 0 */d * *`; anything
    else rounds DOWN to the nearest of those (never slower than asked)."""
    s = max(60, int(seconds))
    m = s // 60
    if m < 60:
        while 60 % m:
            m -= 1
        return f"*/{m} * * * *"
    h = m // 60
    if h < 24:
        while 24 % h:
            h -= 1
        return f"0 */{h} * * *"
    d = min(28, h // 24)
    return f"0 0 */{d} * *"


# §17.1078 — the three APScheduler interval jobs, as periodic tasks. Each one
# is the same coroutine the in-process scheduler would have called; the valve
# that enables the FEATURE still applies (the task is a no-op when it is off).

@app.periodic(cron=cron_from_seconds(settings.alert_eval_interval_seconds))
@app.task(name="scaffold.threshold_eval", queue="maintenance", retry=1, lock="threshold_eval")
async def threshold_eval(timestamp: int) -> dict:
    if not settings.alert_eval_enabled:
        return {"skipped": "alert_eval_enabled=false", "ts": timestamp}
    from app.observability import thresholds
    await thresholds.tick()
    return {"ran": "threshold_eval", "ts": timestamp}


@app.periodic(cron=cron_from_seconds(settings.calibration_watchdog_interval_seconds))
@app.task(name="scaffold.calibration_watchdog", queue="maintenance", retry=1, lock="calibration_watchdog")
async def calibration_watchdog(timestamp: int) -> dict:
    if not settings.calibration_watchdog_enabled:
        return {"skipped": "calibration_watchdog_enabled=false", "ts": timestamp}
    from app.observability import calibration_watchdog as watchdog
    await watchdog.tick()
    return {"ran": "calibration_watchdog", "ts": timestamp}


async def _succeeded_within(task_name: str, seconds: int) -> float | None:
    """Seconds since this task last SUCCEEDED, when that is inside ``seconds``
    — read from procrastinate's own events table, so it survives worker
    restarts. A periodic task's first worker start defers the current cron
    tick immediately; for a cheap sweep that is fine, for a week-long cadence
    of real model calls it is a full learning cycle on every deploy."""
    from sqlalchemy import text
    from app.database import async_session
    async with async_session() as db:
        row = (await db.execute(text("""
            SELECT EXTRACT(EPOCH FROM (now() - max(e.at)))
              FROM procrastinate_events e JOIN procrastinate_jobs j ON j.id = e.job_id
             WHERE j.task_name = :t AND e.type = 'succeeded'
        """), {"t": task_name})).scalar()
    if row is None:
        return None
    ago = float(row)
    return ago if ago < seconds else None


@app.periodic(cron=cron_from_seconds(settings.model_role_learning_interval_seconds))
@app.task(name="scaffold.model_role_learning", queue="learning", retry=1, lock="model_role_learning")
async def model_role_learning(timestamp: int) -> dict:
    if not settings.model_role_learning_enabled:
        return {"skipped": "model_role_learning_enabled=false", "ts": timestamp}
    # §17.1078 — never more often than the configured interval, whatever the
    # worker's restart history (live: the first one-shot worker started a
    # weekly golden re-A/B within seconds of coming up).
    ago = await _succeeded_within("scaffold.model_role_learning", int(settings.model_role_learning_interval_seconds * 0.9))
    if ago is not None:
        return {"skipped": f"last success {int(ago)}s ago < interval", "ts": timestamp}
    from app.modules import model_role_learning as mrl
    await mrl.tick()
    return {"ran": "model_role_learning", "ts": timestamp}


@app.periodic(cron=settings.queue_zombie_run_cron)
@app.task(name="scaffold.zombie_run_sweep", queue="maintenance", retry=1, lock="zombie_run_sweep")
async def zombie_run_sweep(timestamp: int) -> dict:
    """§17.875's startup sweep marks every 'running' row dead because at boot
    they all are. Between boots the only safe criterion is AGE: a turn run
    older than `queue_zombie_run_max_age_minutes` (turns take 1–3 min; the
    longest legitimate one is well under 30) is wedged, and its tail is
    following a corpse."""
    from app.modules.assist_turn import sweep_zombie_runs
    n = await sweep_zombie_runs(older_than_minutes=settings.queue_zombie_run_max_age_minutes)
    logger.info("queue_zombie_run_sweep: marked=%d older_than_min=%d", n, settings.queue_zombie_run_max_age_minutes)
    return {"marked": n, "ts": timestamp}


async def apply_schema() -> None:
    async with app.open_async():
        await app.schema_manager.apply_schema_async()
