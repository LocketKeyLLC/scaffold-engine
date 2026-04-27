"""Tests for app/middleware/performance.py — request timing + model metrics.

Covers:
- HTTP middleware passes through and sets X-Request-Duration-Ms
- /health polling gated to DEBUG when fast, INFO when slow
- _truncate handles None, short, at-limit, over-limit
- log_model_call inserts rows with truncated model/endpoint and 500-char
  error_message cap
- log_model_call swallows DB failures (does not raise to caller)
"""
from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.performance import (
    PerformanceMiddleware,
    _truncate,
    _MODEL_MAX,
    _ENDPOINT_MAX,
    _HEALTH_SLOW_MS,
    log_model_call,
)


# ---------- _truncate pure-function tests ----------

def test_truncate_none_returns_none():
    assert _truncate(None, 10) is None


def test_truncate_under_limit_unchanged():
    assert _truncate("abc", 10) == "abc"


def test_truncate_at_limit_unchanged():
    s = "x" * 10
    assert _truncate(s, 10) == s


def test_truncate_over_limit_uses_ellipsis():
    """Over the limit, we get (limit-1) chars plus an ellipsis = limit chars."""
    s = "x" * 50
    out = _truncate(s, 10)
    assert len(out) == 10
    assert out.endswith("…")
    assert out == "x" * 9 + "…"


# ---------- HTTP middleware tests ----------

@pytest.fixture
def fast_app():
    app = FastAPI()
    app.add_middleware(PerformanceMiddleware)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/work")
    async def work():
        return {"done": True}

    return app


def test_pass_through_sets_duration_header(fast_app):
    client = TestClient(fast_app)
    r = client.get("/work")
    assert r.status_code == 200
    assert "X-Request-Duration-Ms" in r.headers
    # Header value must be parseable as int and non-negative.
    assert int(r.headers["X-Request-Duration-Ms"]) >= 0


def test_health_fast_logs_debug(fast_app, caplog):
    """Fast /health response (<200ms threshold) → DEBUG, not INFO."""
    caplog.set_level(logging.DEBUG, logger="scaffold.perf")
    client = TestClient(fast_app)
    client.get("/health")

    perf_records = [r for r in caplog.records if r.name == "scaffold.perf"]
    assert perf_records, "expected a scaffold.perf log record"
    assert perf_records[-1].levelno == logging.DEBUG, (
        f"fast /health should log at DEBUG, got {perf_records[-1].levelname}"
    )


def test_non_health_logs_info_regardless_of_speed(fast_app, caplog):
    """A non-/health endpoint always logs INFO even when fast."""
    caplog.set_level(logging.DEBUG, logger="scaffold.perf")
    client = TestClient(fast_app)
    client.get("/work")

    perf_records = [r for r in caplog.records if r.name == "scaffold.perf"]
    assert perf_records[-1].levelno == logging.INFO


def test_slow_health_logs_info(fast_app, caplog):
    """When a /health response exceeds threshold, log level is INFO.
    Setting threshold to 0 forces 'slow' classification deterministically
    without faking time (which trips up uvicorn's internal monotonic calls)."""
    caplog.set_level(logging.DEBUG, logger="scaffold.perf")
    with patch("app.middleware.performance._HEALTH_SLOW_MS", 0):
        client = TestClient(fast_app)
        client.get("/health")

    perf_records = [r for r in caplog.records if r.name == "scaffold.perf"]
    assert perf_records, "expected a scaffold.perf log record"
    assert perf_records[-1].levelno == logging.INFO


# ---------- log_model_call tests ----------

@pytest.fixture
def mock_perf_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.middleware.performance.async_session", return_value=cm):
        yield session


@pytest.mark.asyncio
async def test_log_model_call_truncates_model_and_endpoint(mock_perf_session):
    long_model = "m" * (_MODEL_MAX + 50)
    long_endpoint = "e" * (_ENDPOINT_MAX + 50)
    await log_model_call(
        model=long_model,
        endpoint=long_endpoint,
        total_duration_ms=42,
    )
    bind = mock_perf_session.execute.await_args.args[1]
    assert len(bind["model"]) == _MODEL_MAX
    assert len(bind["endpoint"]) == _ENDPOINT_MAX
    assert bind["model"].endswith("…")
    assert bind["endpoint"].endswith("…")


@pytest.mark.asyncio
async def test_log_model_call_truncates_error_message_to_500(mock_perf_session):
    await log_model_call(
        model="m", endpoint="e",
        success=False,
        error_message="x" * 1000,
    )
    bind = mock_perf_session.execute.await_args.args[1]
    assert len(bind["error_message"]) == 500


@pytest.mark.asyncio
async def test_log_model_call_swallows_db_failure():
    """A DB error inside log_model_call must not raise to the caller —
    losing a perf log is acceptable; crashing the calling request is not."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=RuntimeError("db is down"))
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("app.middleware.performance.async_session", return_value=cm):
        # Must not raise.
        await log_model_call(model="m", endpoint="e", total_duration_ms=1)
