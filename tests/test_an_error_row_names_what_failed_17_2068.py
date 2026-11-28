"""§17.2068 — an error_logs row written by the HTTP error middleware names what failed.

Live evidence: 58 of 59 middleware rows had ``error_message = ''`` and a
4000-char ``stack_trace`` cut from the HEAD, so the final exception line was
gone. The real exception was asyncpg's 2 s connect timeout: ``asyncio.timeout``
turns its internal ``CancelledError`` into a bare ``TimeoutError`` (``str() ==
''``) — classified 'unrecoverable', with no method/path in the row.

These tests drive the PRODUCTION ``ErrorLoggingMiddleware`` through a tiny
FastAPI app; only ``async_session`` is patched, to capture the INSERT params.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc
from starlette.requests import Request

from app.middleware.error_logging import ErrorLoggingMiddleware, _classify_error
from app.middleware.request_id import RequestIdMiddleware


@pytest.fixture
def captured():
    """Patch only async_session; return the session mock whose execute()
    receives the INSERT's bind params."""
    session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch("app.middleware.error_logging.async_session", return_value=cm):
        yield session


def _row(session) -> dict:
    session.execute.assert_awaited_once()
    return session.execute.await_args.args[1]


async def _silent_pg_connect(timeout: float) -> None:
    """Open a real asyncpg connection to a socket that accepts and never
    answers — the same path the live row's traceback ends in."""
    held = []

    async def _hold(reader, writer):
        held.append(writer)
        await asyncio.sleep(30)

    server = await asyncio.start_server(_hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await asyncpg.connect(
            host="127.0.0.1", port=port, user="nobody", password="pw-not-real",
            database="nothing", ssl=False, timeout=timeout,
        )
    finally:
        for w in held:
            w.close()
        server.close()


def _deep(n: int) -> None:
    """A chain of n exceptions, each a distinct traceback block (Python
    collapses plain repeated recursion frames, so recursion alone stays short)."""
    if n == 0:
        raise RuntimeError("innermost-cause")
    try:
        _deep(n - 1)
    except RuntimeError as exc:
        raise RuntimeError("final-exception-marker" if n == 40 else f"level-{n}") from exc


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ErrorLoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/jobs")
    async def jobs():
        await _silent_pg_connect(timeout=0.3)

    @app.get("/deep")
    async def deep():
        _deep(40)

    @app.post("/bare")
    async def bare():
        raise TimeoutError

    return app


def test_a_db_connect_timeout_row_names_the_type_the_request_and_is_a_timeout(captured):
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.get("/jobs", headers={"X-Request-ID": "req-2068-abc"})
    assert r.status_code == 500
    row = _row(captured)
    msg = row["error_message"]
    assert msg, "an error row's message must never be empty"
    assert "TimeoutError" in msg
    assert "CancelledError" in msg  # the cause is named too
    assert "GET /jobs" in msg
    assert "req-2068-abc" in msg
    assert row["error_type"] == "timeout"
    # the wire message is not empty either
    assert "TimeoutError" in r.json()["message"]


def test_a_bare_exception_with_no_message_still_names_its_type(captured):
    client = TestClient(_app(), raise_server_exceptions=False)
    client.post("/bare")
    row = _row(captured)
    assert row["error_message"].startswith("POST /bare")
    assert "TimeoutError" in row["error_message"]
    assert "<no message>" in row["error_message"]
    assert row["error_type"] == "timeout"


def test_a_long_traceback_keeps_its_tail_the_exception_line_survives(captured):
    client = TestClient(_app(), raise_server_exceptions=False)
    client.get("/deep")
    tb = _row(captured)["stack_trace"]
    assert len(tb) <= 4200
    assert tb.rstrip().endswith("RuntimeError: final-exception-marker")
    assert "in _deep" in tb[-1500:]  # innermost frames kept
    assert tb.startswith("Traceback")  # and the head
    assert "truncated" in tb


def test_a_short_traceback_is_stored_whole(captured):
    client = TestClient(_app(), raise_server_exceptions=False)
    client.post("/bare")
    tb = _row(captured)["stack_trace"]
    assert "truncated" not in tb
    assert tb.rstrip().endswith("TimeoutError")


def test_a_cancelled_request_propagates_and_writes_no_row(captured):
    """CancelledError is a BaseException: the middleware must neither record
    it as a 500 nor swallow it (guard — this already held on main)."""
    mw = ErrorLoggingMiddleware(app=MagicMock())
    request = Request({"type": "http", "method": "GET", "path": "/jobs",
                       "headers": [], "query_string": b""})

    async def call_next(_req):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mw.dispatch(request, call_next))
    captured.execute.assert_not_awaited()


@pytest.mark.parametrize("exc, expected", [
    (TimeoutError(), "timeout"),
    (asyncio.TimeoutError(), "timeout"),
    (sa_exc.TimeoutError("QueuePool limit reached, connection timed out"), "timeout"),
    (ConnectionRefusedError(111, "Connect call failed"), "transient"),
    (ConnectionResetError(), "transient"),
    (RuntimeError("bug"), "unrecoverable"),
])
def test_db_timeouts_and_refused_connections_are_not_unrecoverable(exc, expected):
    assert _classify_error(exc) == expected
