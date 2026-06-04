"""Tests for app/middleware/performance.py — HTTP request timing.

Covers:
- HTTP middleware passes through and sets X-Request-Duration-Ms
- /health polling gated to DEBUG when fast, INFO when slow
- non-/health paths always log INFO

The module also used to expose `log_model_call` + `_truncate` and the
tests for those lived here. X.22 dropped both (replaced by `_record_call`
into `llm_call_logs` since J.3.a) so the corresponding test cases are
gone alongside the production code.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.performance import PerformanceMiddleware


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


# ---------- §17.411 — 404 cardinality guard ----------

def test_matched_route_uses_path_template(fast_app):
    """A matched route records its template (here the literal /work, which
    is also the template since it has no path params)."""
    captured = []
    with patch(
        "app.observability.metrics.record_http_request",
        side_effect=lambda **kw: captured.append(kw),
    ):
        client = TestClient(fast_app)
        client.get("/work")
    assert captured, "expected a record_http_request call"
    assert captured[-1]["path_template"] == "/work"
    assert captured[-1]["status"] == 200


def test_unmatched_path_buckets_under_sentinel(fast_app):
    """Unrouted 404 paths must collapse to the '__unmatched__' sentinel so a
    fuzzer can't explode http_requests_total label cardinality (E1→§17.411)."""
    captured = []
    with patch(
        "app.observability.metrics.record_http_request",
        side_effect=lambda **kw: captured.append(kw),
    ):
        client = TestClient(fast_app)
        # Two distinct unmatched paths — both must report the SAME template.
        client.get("/no/such/path/aaa")
        client.get("/no/such/path/bbb-different")
    assert len(captured) == 2
    templates = {c["path_template"] for c in captured}
    assert templates == {"__unmatched__"}, (
        f"unmatched paths leaked literal URLs into the metric label: {templates}"
    )
    assert all(c["status"] == 404 for c in captured)
