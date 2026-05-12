"""Tests for resume_cancelled_job + POST /jobs/{job_id}/resume.

Two levels:
  - Unit: handler against a mocked AsyncSession — verifies the atomic
    UPDATE shape + the three outcome branches.
  - Integration: TestClient against the real FastAPI app — verifies the
    endpoint maps outcomes to HTTP correctly and delegates to
    execute_all_nodes when the transition succeeds.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.modules import execution_handler


# ---------------------------------------------------------------------------
# Unit — resume_cancelled_job
# ---------------------------------------------------------------------------

def _mock_db_for_update(updated_rows, status_lookup=None):
    """Build a db where execute() returns:
       1st call → UPDATE ... RETURNING result (with updated_rows)
       2nd call → SELECT status result (with status_lookup)
    """
    update_result = MagicMock()
    update_result.fetchone.return_value = updated_rows
    if status_lookup is not None:
        status_result = MagicMock()
        status_result.fetchone.return_value = status_lookup
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[update_result, status_result])
    else:
        db = AsyncMock()
        db.execute = AsyncMock(return_value=update_result)
    return db


@pytest.mark.asyncio
async def test_resume_happy_path_returns_resumed():
    job_id = uuid4()
    db = _mock_db_for_update(SimpleNamespace(id=str(job_id)))
    out = await execution_handler.resume_cancelled_job(job_id, db)
    assert out == {
        "outcome": "resumed",
        "job_id": str(job_id),
        "prior_status": "cancelled",
    }
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_not_found():
    """UPDATE returns no row AND status SELECT returns no row."""
    job_id = uuid4()
    db = _mock_db_for_update(updated_rows=None, status_lookup=None)
    out = await execution_handler.resume_cancelled_job(job_id, db)
    assert out == {"outcome": "not_found", "job_id": str(job_id)}
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_wrong_status_completed():
    """UPDATE returns no row, status SELECT shows job is completed."""
    job_id = uuid4()
    db = _mock_db_for_update(
        updated_rows=None,
        status_lookup=SimpleNamespace(status="completed"),
    )
    out = await execution_handler.resume_cancelled_job(job_id, db)
    assert out == {
        "outcome": "wrong_status",
        "job_id": str(job_id),
        "current_status": "completed",
    }
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_wrong_status_executing():
    """A job already executing must not be re-resumed."""
    job_id = uuid4()
    db = _mock_db_for_update(
        updated_rows=None,
        status_lookup=SimpleNamespace(status="executing"),
    )
    out = await execution_handler.resume_cancelled_job(job_id, db)
    assert out["outcome"] == "wrong_status"
    assert out["current_status"] == "executing"


@pytest.mark.asyncio
async def test_resume_update_sql_targets_cancelled_only():
    """The UPDATE must be gated on status='cancelled' so two concurrent
    callers compete safely. Verify the bind params + the SQL shape."""
    job_id = uuid4()
    db = _mock_db_for_update(SimpleNamespace(id=str(job_id)))
    await execution_handler.resume_cancelled_job(job_id, db)

    first_call = db.execute.await_args_list[0]
    sql_obj = first_call.args[0]
    sql_text = str(sql_obj)
    assert "UPDATE jobs" in sql_text
    assert "status = 'executing'" in sql_text
    assert "status = 'cancelled'" in sql_text  # the WHERE guard
    assert "RETURNING id" in sql_text
    # Bind params carry the right job_id
    assert first_call.args[1] == {"job_id": str(job_id)}


# ---------------------------------------------------------------------------
# Integration — POST /jobs/{job_id}/resume
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


@pytest.fixture
def patch_models_ok():
    """_require_valid_models is async + raises on missing — short-circuit it."""
    with patch("app.main._require_valid_models", new_callable=AsyncMock) as m:
        m.return_value = None
        yield m


def test_resume_endpoint_404_on_unknown_job(client, patch_models_ok):
    """An unknown job_id surfaces as 404."""
    job_id = str(uuid4())
    with patch(
        "app.main.resume_cancelled_job",
        new=AsyncMock(return_value={"outcome": "not_found", "job_id": job_id}),
    ):
        r = client.post(f"/jobs/{job_id}/resume", json={})
    assert r.status_code == 404
    assert job_id in r.json()["detail"]


def test_resume_endpoint_409_on_wrong_status(client, patch_models_ok):
    """A non-cancelled job surfaces as 409 with current_status in detail."""
    job_id = str(uuid4())
    with patch(
        "app.main.resume_cancelled_job",
        new=AsyncMock(return_value={
            "outcome": "wrong_status",
            "job_id": job_id,
            "current_status": "completed",
        }),
    ):
        r = client.post(f"/jobs/{job_id}/resume", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["current_status"] == "completed"
    assert detail["expected_status"] == "cancelled"


def test_resume_endpoint_400_on_bad_uuid(client, patch_models_ok):
    r = client.post("/jobs/not-a-uuid/resume", json={})
    assert r.status_code == 400
    assert "Invalid job_id" in r.json()["detail"]


def test_resume_endpoint_streams_on_success(client, patch_models_ok):
    """Happy path: handler returns resumed → execute_all_nodes is invoked
    and the response is an SSE stream."""
    job_id = str(uuid4())

    async def _fake_stream(job_id_arg, model_overrides=None):
        yield "event: started\ndata: {}\n\n"
        yield "event: done\ndata: {}\n\n"

    with patch(
        "app.main.resume_cancelled_job",
        new=AsyncMock(return_value={
            "outcome": "resumed",
            "job_id": job_id,
            "prior_status": "cancelled",
        }),
    ), patch("app.main.execute_all_nodes", new=_fake_stream):
        r = client.post(f"/jobs/{job_id}/resume", json={})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: started" in body
    assert "event: done" in body


def test_resume_endpoint_passes_model_overrides(client, patch_models_ok):
    """Body's model_overrides must be forwarded to execute_all_nodes."""
    job_id = str(uuid4())
    captured: dict = {}

    async def _fake_stream(job_id_arg, model_overrides=None):
        captured["job_id"] = job_id_arg
        captured["model_overrides"] = model_overrides
        yield "event: ping\ndata: {}\n\n"

    with patch(
        "app.main.resume_cancelled_job",
        new=AsyncMock(return_value={
            "outcome": "resumed",
            "job_id": job_id,
            "prior_status": "cancelled",
        }),
    ), patch("app.main.execute_all_nodes", new=_fake_stream):
        r = client.post(
            f"/jobs/{job_id}/resume",
            json={"model_overrides": {"model_general": "custom:7b"}},
        )

    assert r.status_code == 200
    assert captured["job_id"] == job_id
    assert captured["model_overrides"] == {"model_general": "custom:7b"}


def test_resume_endpoint_validates_models_before_db(client):
    """_require_valid_models must be awaited before the DB UPDATE.
    A 422 from model validation must NOT mutate job state."""
    job_id = str(uuid4())
    exc = HTTPException(
        status_code=422,
        detail={"error": "model_validation_failed", "missing_models": ["bad:1b"]},
    )
    db_called = False

    async def _fake_resume(*args, **kwargs):
        nonlocal db_called
        db_called = True
        return {"outcome": "resumed"}

    with patch("app.main._require_valid_models",
               new_callable=AsyncMock, side_effect=exc), \
         patch("app.main.resume_cancelled_job", new=_fake_resume):
        r = client.post(
            f"/jobs/{job_id}/resume",
            json={"model_overrides": {"model_general": "bad:1b"}},
        )

    assert r.status_code == 422
    assert db_called is False
