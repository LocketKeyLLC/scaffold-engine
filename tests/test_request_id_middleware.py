"""Tests for app/middleware/request_id.py — correlation ID binding.

Covers:
- Inbound X-Request-ID is honored (passed through to response)
- Missing header triggers a fresh UUID4 hex (32 chars)
- contextvar is bound during the request and cleared after
- Each request gets a distinct generated ID (no leak across requests)
"""
from __future__ import annotations

import re

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.request_id import RequestIdMiddleware


HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def app():
    """Minimal FastAPI app with the middleware + an introspection endpoint
    that returns the currently-bound request_id from contextvars.
    """
    a = FastAPI()
    a.add_middleware(RequestIdMiddleware)

    @a.get("/probe")
    async def probe():
        ctx = structlog.contextvars.get_contextvars()
        return {"bound_request_id": ctx.get("request_id")}

    return a


@pytest.fixture
def client(app):
    return TestClient(app)


def test_honors_inbound_x_request_id(client):
    """When the client sends X-Request-ID, the same value is echoed back
    AND is the value bound during the request."""
    r = client.get("/probe", headers={"X-Request-ID": "client-trace-abc-123"})
    assert r.status_code == 200
    assert r.headers["X-Request-ID"] == "client-trace-abc-123"
    assert r.json()["bound_request_id"] == "client-trace-abc-123"


def test_generates_uuid4_hex_when_missing(client):
    """Missing inbound header → 32-char lowercase hex (uuid4().hex)."""
    r = client.get("/probe")
    assert r.status_code == 200
    rid = r.headers["X-Request-ID"]
    assert HEX32.match(rid), f"expected 32-char hex, got: {rid!r}"
    assert r.json()["bound_request_id"] == rid


def test_each_request_gets_distinct_generated_id(client):
    """Two requests without inbound header → two different IDs (no leak)."""
    r1 = client.get("/probe")
    r2 = client.get("/probe")
    assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]
    assert r1.json()["bound_request_id"] != r2.json()["bound_request_id"]


def test_contextvar_cleared_after_request(client):
    """After the request returns, the contextvar must NOT still be bound
    in the outer test scope. (Prevents leak across tasks/requests.)"""
    client.get("/probe", headers={"X-Request-ID": "should-not-leak"})
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("request_id") is None


def test_empty_inbound_header_treated_as_missing(client):
    """An empty X-Request-ID string falls back to a generated UUID rather
    than echoing an empty string. Defensive: empty IDs break log greps."""
    r = client.get("/probe", headers={"X-Request-ID": ""})
    assert r.status_code == 200
    rid = r.headers["X-Request-ID"]
    assert HEX32.match(rid), f"empty inbound should generate UUID, got: {rid!r}"
