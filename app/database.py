"""Async database connection and session management."""
import sys
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

# §17.808 — under pytest, `with TestClient(app)` runs the app lifespan on a
# FRESH event loop per instance (Starlette drives it through an anyio
# BlockingPortal). A pooled asyncpg connection created on one test's loop and
# then reused by the next test's lifespan raises "attached to a different loop"
# / "Connection._cancel was never awaited" — which surfaced as
# `migrations_hook_crashed` plus ~99 web-test setup ERRORs in the §17.807
# baseline. NullPool makes every checkout a fresh connection bound to the
# CURRENT loop and fully closed on return, so nothing crosses loops. This
# mirrors the per-test httpx-client re-seed in tests/conftest.py
# (`_init_shared_http_clients`). The live orchestrator process imports this
# module WITHOUT pytest loaded, so production keeps the real connection pool —
# `"pytest" in sys.modules` is true only inside the test process, and is
# already set by the time conftest eager-loads `app` at collection start.
_UNDER_PYTEST = "pytest" in sys.modules

_engine_kwargs: dict = {
    "echo": (settings.log_level.lower() == "debug"),
    # §17.179 — cap the asyncpg connect handshake. Default is 60 s, which under
    # an unreachable Postgres host (e.g. cloud-CI smoke where `scaffold-postgres`
    # is NXDOMAIN) makes every async_session() open block for a full minute
    # before raising. Healthy localhost connects complete in milliseconds.
    # §17.179 follow-up (2026-05-23): lowered 5 → 2 s to match the tightened
    # lifespan probe cap (_STARTUP_PROBE_TIMEOUT_S=2.0 in app/main.py). This is
    # the connect timeout only — per-query budgets stay unbounded.
    "connect_args": {"timeout": 2},
}
if _UNDER_PYTEST:
    # No pool → no connection survives to be reused on a different loop.
    # pool_size/max_overflow are invalid with NullPool, so they're omitted here.
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20
    _engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async database session.

    Callers commit explicitly. Teardown rolls back on ANY unhandled
    exception, including ``asyncio.CancelledError``.

    §17.274 — must catch ``BaseException``, not ``Exception``.
    ``CancelledError`` is a ``BaseException`` subclass (not Exception);
    Starlette raises it when the client disconnects mid-request. The
    pre-§17.274 ``except Exception`` would let CancelledError leak past
    the rollback, leaving the transaction in an inconsistent state and
    holding row-level locks until the connection's eventual GC.
    """
    async with async_session() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
