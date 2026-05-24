"""Tests for app/routers/observability.py.

§17.278 closes the §17.273 test-gap: the router had no test file.
Covers the four endpoints plus the auth-gating + parameter-validation
contracts FastAPI auto-enforces via Query() + the explicit UUID guard.

Strategy:
  - Mock app.modules.observability_rollups.* so the router's
    delegating call returns a known shape.
  - Mock get_db via dependency_overrides for the PATCH endpoint (which
    runs SQL directly).
  - Override require_api_key for every test (auth surface is covered by
    tests/test_auth.py).
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.database import get_db
from app.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """TestClient with auth bypassed (covered elsewhere)."""
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


@pytest.fixture
def mock_db():
    """Override get_db so PATCH /observability/errors/{id} sees a mock."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _gen():
        yield db

    app.dependency_overrides[get_db] = _gen
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# GET /observability/llm
# ---------------------------------------------------------------------------

def test_llm_rollup_delegates_to_module(client):
    """Happy path: returns whatever observability_rollups.llm_rollup returns."""
    payload = {"window_minutes": 60, "rows": [{"provider": "ollama", "model": "qwen3:4b", "calls": 5}]}
    with patch(
        "app.routers.observability.observability_rollups.llm_rollup",
        new=AsyncMock(return_value=payload),
    ) as m:
        r = client.get("/observability/llm?window_minutes=60")
    assert r.status_code == 200
    assert r.json() == payload
    m.assert_awaited_once()


def test_llm_rollup_forwards_provider_and_model_filters(client):
    """Query params provider= and model= reach the module call."""
    with patch(
        "app.routers.observability.observability_rollups.llm_rollup",
        new=AsyncMock(return_value={"rows": []}),
    ) as m:
        r = client.get("/observability/llm?provider=openai&model=gpt-4o")
    assert r.status_code == 200
    kwargs = m.await_args.kwargs
    assert kwargs["provider"] == "openai"
    assert kwargs["model"] == "gpt-4o"


@pytest.mark.parametrize("window", [0, 10081, -1])
def test_llm_rollup_window_validation(client, window):
    """Query() ge=1 le=10080 enforces the window bounds."""
    r = client.get(f"/observability/llm?window_minutes={window}")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /observability/errors
# ---------------------------------------------------------------------------

def test_recent_errors_default_returns_all(client):
    payload = {"errors": [], "total": 0}
    with patch(
        "app.routers.observability.observability_rollups.recent_errors",
        new=AsyncMock(return_value=payload),
    ) as m:
        r = client.get("/observability/errors")
    assert r.status_code == 200
    assert r.json() == payload
    kwargs = m.await_args.kwargs
    # Default: resolved=None, since_minutes=None, limit=50
    assert kwargs["resolved"] is None
    assert kwargs["since_minutes"] is None
    assert kwargs["limit"] == 50


def test_recent_errors_filter_unresolved(client):
    with patch(
        "app.routers.observability.observability_rollups.recent_errors",
        new=AsyncMock(return_value={"errors": []}),
    ) as m:
        r = client.get("/observability/errors?resolved=false&since_minutes=60&limit=10")
    assert r.status_code == 200
    kwargs = m.await_args.kwargs
    assert kwargs["resolved"] is False
    assert kwargs["since_minutes"] == 60
    assert kwargs["limit"] == 10


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_recent_errors_limit_validation(client, limit):
    r = client.get(f"/observability/errors?limit={limit}")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /observability/jobs
# ---------------------------------------------------------------------------

def test_recent_jobs_delegates(client):
    payload = {"jobs": [{"id": "j1", "total_cost_usd": 0.42}], "total": 1}
    with patch(
        "app.routers.observability.observability_rollups.recent_jobs_costs",
        new=AsyncMock(return_value=payload),
    ) as m:
        r = client.get("/observability/jobs?window_minutes=120&limit=10")
    assert r.status_code == 200
    assert r.json() == payload
    kwargs = m.await_args.kwargs
    assert kwargs["window_minutes"] == 120
    assert kwargs["limit"] == 10


@pytest.mark.parametrize("limit", [0, 201])
def test_recent_jobs_limit_validation(client, limit):
    r = client.get(f"/observability/jobs?limit={limit}")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /observability/errors/{error_id}
# ---------------------------------------------------------------------------

def test_resolve_error_invalid_uuid_returns_422(client, mock_db):
    """The UUID validator at the endpoint top short-circuits before any DB call."""
    r = client.patch(
        "/observability/errors/not-a-uuid",
        json={"resolved": True, "resolution": "fixed"},
    )
    assert r.status_code == 422
    assert "UUID" in r.json()["detail"]
    mock_db.execute.assert_not_awaited()


def test_resolve_error_not_found_returns_404(client, mock_db):
    """Valid UUID but no row → 404."""
    res = MagicMock()
    res.fetchone.return_value = None
    mock_db.execute = AsyncMock(return_value=res)

    error_id = str(uuid4())
    r = client.patch(
        f"/observability/errors/{error_id}",
        json={"resolved": True, "resolution": "fixed"},
    )
    assert r.status_code == 404
    assert error_id in r.json()["detail"]
    mock_db.commit.assert_not_awaited()


def test_resolve_error_happy_path_returns_resolved_payload(client, mock_db):
    """Valid UUID + row → 200 with the ErrorLogResolveResponse shape."""
    error_id = uuid4()
    resolved_at = _dt.datetime(2026, 5, 24, 10, 0, 0)
    row = SimpleNamespace(
        id=error_id,
        resolved=True,
        resolution="fixed by restart",
        resolved_at=resolved_at,
    )
    res = MagicMock()
    res.fetchone.return_value = row
    mock_db.execute = AsyncMock(return_value=res)

    r = client.patch(
        f"/observability/errors/{error_id}",
        json={"resolved": True, "resolution": "fixed by restart"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["error_id"] == str(error_id)
    assert body["resolved"] is True
    assert body["resolution"] == "fixed by restart"
    assert body["resolved_at"] == resolved_at.isoformat()
    mock_db.commit.assert_awaited_once()


def test_resolve_error_unresolve_clears_resolved_at(client, mock_db):
    """Body resolved=false → resolved_at=None in the SQL + response."""
    error_id = uuid4()
    row = SimpleNamespace(
        id=error_id,
        resolved=False,
        resolution=None,
        resolved_at=None,
    )
    res = MagicMock()
    res.fetchone.return_value = row
    mock_db.execute = AsyncMock(return_value=res)

    r = client.patch(
        f"/observability/errors/{error_id}",
        json={"resolved": False, "resolution": None},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] is False
    assert body["resolved_at"] is None
