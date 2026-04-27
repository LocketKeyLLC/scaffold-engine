"""Tests for app/middleware/error_logging.py — exception capture + classify.

Covers:
- Successful requests pass through unchanged
- Unhandled exceptions are caught and surfaced as structured 500 responses
- Exception classification (_classify_error) maps to the correct error_type
- Errors are persisted to the error_logs table
- A failure in error_logs persistence does NOT break the response (the
  middleware swallows the secondary failure so the user still gets the
  classified 500 instead of a bare ASGI crash)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.middleware.error_logging import ErrorLoggingMiddleware, _classify_error


# ---------- _classify_error pure-function tests ----------

def test_classify_timeout_returns_timeout():
    assert _classify_error(httpx.TimeoutException("slow")) == "timeout"


def test_classify_connect_timeout_is_timeout_subclass():
    """ConnectTimeout is a TimeoutException subclass — should classify same."""
    assert _classify_error(httpx.ConnectTimeout("conn slow")) == "timeout"


def test_classify_http_error_returns_transient():
    """Generic HTTPError (non-timeout) → transient."""
    assert _classify_error(httpx.HTTPError("network blip")) == "transient"


@pytest.mark.parametrize("exc", [
    ValueError("bad input"),
    TypeError("wrong type"),
    KeyError("missing"),
])
def test_classify_validation_family(exc):
    assert _classify_error(exc) == "validation"


def test_classify_unknown_returns_unrecoverable():
    assert _classify_error(RuntimeError("???")) == "unrecoverable"


# ---------- middleware integration tests ----------

@pytest.fixture
def app_with_endpoints():
    """App with a healthy endpoint and an exploding endpoint."""
    app = FastAPI()
    app.add_middleware(ErrorLoggingMiddleware)

    @app.get("/ok")
    async def ok():
        return {"status": "ok"}

    @app.get("/explode")
    async def explode():
        raise ValueError("kaboom")

    @app.get("/timeout")
    async def explode_timeout():
        raise httpx.TimeoutException("upstream slow")

    return app


@pytest.fixture
def mock_session():
    """Patch async_session so the middleware doesn't touch a real DB.
    Returns the AsyncMock for assertion access."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.middleware.error_logging.async_session",
        return_value=cm,
    ):
        yield session


def test_passes_through_successful_response(app_with_endpoints, mock_session):
    client = TestClient(app_with_endpoints)
    r = client.get("/ok")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    # No DB write on success.
    mock_session.execute.assert_not_awaited()


def test_catches_exception_returns_structured_500(app_with_endpoints, mock_session):
    client = TestClient(app_with_endpoints, raise_server_exceptions=False)
    r = client.get("/explode")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "ValueError"
    assert "kaboom" in body["message"]
    assert body["path"] == "/explode"


def test_persists_error_to_error_logs(app_with_endpoints, mock_session):
    """The middleware must call session.execute exactly once with bind
    params containing the classified error_type."""
    client = TestClient(app_with_endpoints, raise_server_exceptions=False)
    client.get("/explode")  # ValueError → "validation"

    mock_session.execute.assert_awaited_once()
    bind = mock_session.execute.await_args.args[1]
    assert bind["error_type"] == "validation"
    assert "kaboom" in bind["error_message"]
    assert bind["stack_trace"]  # non-empty


def test_timeout_classification_threaded_through(app_with_endpoints, mock_session):
    client = TestClient(app_with_endpoints, raise_server_exceptions=False)
    client.get("/timeout")
    bind = mock_session.execute.await_args.args[1]
    assert bind["error_type"] == "timeout"


def test_persistence_failure_does_not_break_response(app_with_endpoints):
    """If error_logs persistence raises, the user MUST still get a 500
    instead of a bare ASGI crash. Regression guard for the case where
    DB is down at the same moment an endpoint fails."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=RuntimeError("db is down"))
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.middleware.error_logging.async_session",
        return_value=cm,
    ):
        client = TestClient(app_with_endpoints, raise_server_exceptions=False)
        r = client.get("/explode")

    assert r.status_code == 500
    assert r.json()["error"] == "ValueError"
