"""Tests for app/database.py — async session lifecycle.

§17.274 closes the §17.273 🔴 finding that get_db() caught Exception
but not BaseException. CancelledError (a BaseException) would leak
past the rollback, leaving the transaction inconsistent.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_session_ctx():
    """Build a mock async-session context manager.

    Returns (session_mock, session_factory_mock) where:
      - session_mock is the AsyncMock used inside the `async with` body
      - session_factory_mock is what you pass to patch the async_session
        callable; calling it returns the context manager.
    """
    session = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=cm)
    return session, factory


@pytest.mark.asyncio
async def test_get_db_rolls_back_on_exception():
    """Baseline (pre-§17.274 behavior, still valid): a plain Exception
    raised inside the with-block triggers rollback."""
    session, factory = _mock_session_ctx()
    with patch("app.database.async_session", factory):
        from app.database import get_db
        gen = get_db()
        async for db in gen:
            assert db is session
            with pytest.raises(ValueError):
                await gen.athrow(ValueError("oops"))
            break

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_db_rolls_back_on_cancelled_error():
    """§17.274 — the load-bearing test. CancelledError is a BaseException,
    not Exception. Pre-fix this would have leaked past the rollback.
    Post-fix the rollback fires + CancelledError re-raises."""
    session, factory = _mock_session_ctx()
    with patch("app.database.async_session", factory):
        from app.database import get_db
        gen = get_db()
        async for db in gen:
            assert db is session
            with pytest.raises(asyncio.CancelledError):
                await gen.athrow(asyncio.CancelledError())
            break

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_db_rolls_back_on_keyboard_interrupt():
    """§17.274 — defense in depth. KeyboardInterrupt is also a BaseException
    (and SystemExit too). Same broad guard catches all of them. Operator
    Ctrl-C during a long query must still cleanly release the transaction."""
    session, factory = _mock_session_ctx()
    with patch("app.database.async_session", factory):
        from app.database import get_db
        gen = get_db()
        async for db in gen:
            assert db is session
            with pytest.raises(KeyboardInterrupt):
                await gen.athrow(KeyboardInterrupt())
            break

    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_db_no_rollback_on_normal_completion():
    """When the caller iterates the generator to natural completion (the
    normal FastAPI teardown path: route returns → dependency runs past
    yield → StopAsyncIteration), no exception fires inside the with-block,
    so rollback is NOT called. Verifies the §17.274 broadening to
    BaseException didn't widen the rollback to clean-completion paths."""
    session, factory = _mock_session_ctx()
    with patch("app.database.async_session", factory):
        from app.database import get_db
        gen = get_db()
        # Drive the generator the way FastAPI does: anext to get the
        # session, then anext again to drain past the yield. The second
        # anext returns no value and the generator exits via the implicit
        # StopAsyncIteration after the `async with` block closes cleanly.
        db = await gen.__anext__()
        assert db is session
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_db_rolls_back_on_generator_exit():
    """§17.274 — `gen.aclose()` raises GeneratorExit (also a BaseException).
    Post-fix this correctly triggers rollback so a cancelled request that
    closes the generator without commit still releases the transaction.
    Pre-fix the rollback was skipped; uncommitted writes lingered until
    connection GC. Harmless when the route already committed (rollback
    is a no-op on a cleanly-committed transaction)."""
    session, factory = _mock_session_ctx()
    with patch("app.database.async_session", factory):
        from app.database import get_db
        gen = get_db()
        db = await gen.__anext__()
        assert db is session
        await gen.aclose()

    session.rollback.assert_awaited_once()
