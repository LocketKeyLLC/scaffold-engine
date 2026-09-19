"""§17.1132 (ledger L-5) — SAVEPOINT for optional DB work. Deliberately free of any
engine import: assist modules import it, and host-side static tests import those
modules without asyncpg installed (app.database builds the engine on import).
"""
from __future__ import annotations

def savepoint(db):
    """§17.1132 (ledger L-5) — ``async with savepoint(db):`` around OPTIONAL
    statements whose failure the caller swallows.

    On a real ``AsyncSession`` this is ``db.begin_nested()`` — a SAVEPOINT: a
    failure inside rolls back only that work and the session stays usable, so
    the next statement does not raise ``PendingRollbackError`` ("Can't reconnect
    until invalid transaction is rolled back" — the 2026-09-13 submit 500).
    On a test double that has no async-context ``begin_nested`` (an AsyncMock
    session) it is a no-op context, so the unit suites keep their plain mocks.
    """
    import contextlib
    import inspect

    factory = getattr(db, "begin_nested", None)
    if factory is None:
        return contextlib.nullcontext()
    ctx = factory()
    if hasattr(ctx, "__aenter__"):
        return ctx
    if inspect.iscoroutine(ctx):
        ctx.close()  # an AsyncMock child — never awaited, never a savepoint
    return contextlib.nullcontext()
