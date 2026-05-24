"""Tests for app/routers/schedule.py.

§17.278 closes the §17.273 test-gap: the router had no test file.
Covers the three endpoints (POST/GET/DELETE) plus the cron-validation
+ model-validation + pagination-bounds contracts.

Strategy mirrors test_router_observability.py:
  - dependency_overrides for require_api_key + get_db
  - Mock app.scheduler.add_schedule + app.scheduler.delete_schedule so
    no real APScheduler interaction happens
  - Mock app.utils.model_validation._require_valid_models so we don't
    need a live Ollama
"""
from __future__ import annotations

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

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
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


@pytest.fixture
def mock_db():
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


@pytest.fixture
def patch_models_ok():
    """_require_valid_models is async + raises on missing — short-circuit it."""
    with patch(
        "app.routers.schedule._require_valid_models",
        new_callable=AsyncMock,
    ) as m:
        m.return_value = None
        yield m


# ---------------------------------------------------------------------------
# POST /schedule
# ---------------------------------------------------------------------------

_VALID_BODY = {
    "topic": "rust async",
    "depth": "medium",
    "cron_expression": "0 9 * * *",
    "timezone": "UTC",
    "model_overrides": {},
}


def _row_for_insert(*, id_=1):
    """Build a SQLAlchemy mappings().first() shape for the schedule INSERT RETURNING."""
    created = _dt.datetime(2026, 5, 24, 10, 0, 0)
    return {
        "id": id_,
        "topic": _VALID_BODY["topic"],
        "depth": _VALID_BODY["depth"],
        "cron_expression": _VALID_BODY["cron_expression"],
        "timezone": _VALID_BODY["timezone"],
        "enabled": True,
        "last_run_at": None,
        "last_status": None,
        "last_job_id": None,
        "next_run_at": None,
        "run_count": 0,
        "failure_count": 0,
        "created_at": created,
    }


def test_create_schedule_happy_path(client, mock_db, patch_models_ok):
    """Valid cron + model overrides → 200, INSERT committed, scheduler registered."""
    res = MagicMock()
    res.mappings.return_value.first.return_value = _row_for_insert()
    mock_db.execute = AsyncMock(return_value=res)

    next_run = _dt.datetime(2026, 5, 25, 9, 0, 0)
    with patch(
        "app.scheduler.add_schedule",
        new_callable=AsyncMock,
        return_value=next_run,
    ) as m_add:
        r = client.post("/schedule", json=_VALID_BODY)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == 1
    assert body["topic"] == _VALID_BODY["topic"]
    assert body["next_run_at"] == next_run.isoformat()
    mock_db.commit.assert_awaited_once()
    mock_db.rollback.assert_not_awaited()
    m_add.assert_awaited_once()


@pytest.mark.parametrize("bad_cron", [
    "not a cron",          # gibberish
    "* * * *",             # 4 fields, not 5
    "61 * * * *",          # minute out of range
])
def test_create_schedule_rejects_invalid_cron(client, mock_db, bad_cron):
    """Bad cron expression → 422 BEFORE any DB call."""
    body = {**_VALID_BODY, "cron_expression": bad_cron}
    r = client.post("/schedule", json=body)
    assert r.status_code == 422
    assert "cron" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()
    mock_db.execute.assert_not_awaited()


def test_create_schedule_rejects_invalid_timezone(client, mock_db):
    """Bad timezone → 422 before DB."""
    body = {**_VALID_BODY, "timezone": "Mars/Olympus"}
    r = client.post("/schedule", json=body)
    assert r.status_code == 422
    mock_db.execute.assert_not_awaited()


def test_create_schedule_scheduler_failure_rolls_back(client, mock_db, patch_models_ok):
    """add_schedule raises → 502, DB rolled back, no commit."""
    res = MagicMock()
    res.mappings.return_value.first.return_value = _row_for_insert()
    mock_db.execute = AsyncMock(return_value=res)

    with patch(
        "app.scheduler.add_schedule",
        new_callable=AsyncMock,
        side_effect=RuntimeError("apscheduler boom"),
    ):
        r = client.post("/schedule", json=_VALID_BODY)

    assert r.status_code == 502
    assert "scheduler registration failed" in r.json()["detail"]
    mock_db.rollback.assert_awaited_once()
    mock_db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# GET /schedule
# ---------------------------------------------------------------------------

def test_list_schedules_returns_paginated_payload(client, mock_db):
    """Two SELECTs (COUNT + page) → response carries total/limit/offset/schedules."""
    count_res = MagicMock()
    count_res.scalar.return_value = 7

    rows_res = MagicMock()
    rows_res.mappings.return_value.all.return_value = [
        _row_for_insert(id_=i) for i in range(3)
    ]
    mock_db.execute = AsyncMock(side_effect=[count_res, rows_res])

    r = client.get("/schedule?limit=3&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 7
    assert body["limit"] == 3
    assert body["offset"] == 0
    assert len(body["schedules"]) == 3


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_list_schedules_limit_bounds(client, mock_db, limit):
    r = client.get(f"/schedule?limit={limit}")
    assert r.status_code == 422
    assert "limit" in r.json()["detail"]


def test_list_schedules_offset_negative(client, mock_db):
    r = client.get("/schedule?offset=-1")
    assert r.status_code == 422
    assert "offset" in r.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /schedule/{schedule_id}
# ---------------------------------------------------------------------------

def test_delete_schedule_happy_path(client, mock_db):
    with patch(
        "app.scheduler.delete_schedule",
        new_callable=AsyncMock,
        return_value=True,
    ) as m_del:
        r = client.delete("/schedule/42")
    assert r.status_code == 200
    assert r.json() == {"deleted": 42}
    mock_db.commit.assert_awaited_once()
    m_del.assert_awaited_once()


def test_delete_schedule_not_found_returns_404(client, mock_db):
    """delete_schedule returns False → 404, no commit."""
    with patch(
        "app.scheduler.delete_schedule",
        new_callable=AsyncMock,
        return_value=False,
    ):
        r = client.delete("/schedule/99999")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()
    mock_db.commit.assert_not_awaited()
