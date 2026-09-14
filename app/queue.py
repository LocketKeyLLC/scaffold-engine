"""§17.1076 — the Postgres task queue (procrastinate), as a trial.

The engine's background work — the stale-job reaper + staleness sweep
(`modules/cleanup.py`), the three APScheduler interval jobs, the detached
run broker's zombie sweep and the resume-on-startup loop — is a durable
job queue built piece by piece: each loop owns its own retry, its own
interval and its own "did the process die mid-task" story. procrastinate
gives one queue on the Postgres the engine already has (no broker), with
retries, locks and periodic tasks declared once.

This trial ports ONE thing — the cleanup sweep as a periodic task — behind
``queue_enabled`` (default off; the asyncio loop keeps running when off).
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


async def apply_schema() -> None:
    async with app.open_async():
        await app.schema_manager.apply_schema_async()
