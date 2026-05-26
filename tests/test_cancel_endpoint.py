"""§17.322 — Tests for cancel_active_job + POST /jobs/{job_id}/cancel.

Mirrors tests/test_resume_endpoint.py's two-tier shape:
  - Unit: handler against a mocked AsyncSession — verifies the
    CTE+UPDATE shape and all four outcome branches (cancelled,
    already_cancelled, wrong_status, not_found).
  - Integration: TestClient against the real FastAPI app — verifies
    the endpoint maps outcomes to HTTP correctly (200 / 200-idempotent
    / 404 / 409 / 422).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.modules import execution_handler


# ---------------------------------------------------------------------------
# Unit — cancel_active_job
# ---------------------------------------------------------------------------

def _mock_db_for_cancel(updated_rows, status_lookup=None):
    """Build a db where execute() returns:
       1st call → UPDATE...RETURNING result (with updated_rows)
       2nd call → SELECT status result (with status_lookup)
    """
    update_result = MagicMock()
    update_result.fetchone.return_value = updated_rows
    if status_lookup is not None or updated_rows is None:
        status_result = MagicMock()
        status_result.fetchone.return_value = status_lookup
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[update_result, status_result])
    else:
        db = AsyncMock()
        db.execute = AsyncMock(return_value=update_result)
    return db


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_cancel_happy_path_returns_cancelled_with_prior_status():
    """An active→cancelled flip returns the prior status (captured via CTE)."""
    job_id = uuid4()
    db = _mock_db_for_cancel(
        SimpleNamespace(id=str(job_id), prior_status="awaiting_confirmation"),
    )
    out = await execution_handler.cancel_active_job(job_id, db)
    assert out == {
        "outcome": "cancelled",
        "job_id": str(job_id),
        "status_before": "awaiting_confirmation",
        "status_after": "cancelled",
    }
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_cancel_carries_prior_status_for_running_job():
    """Cancelling a running job carries the prior status so the operator
    knows what state was interrupted (operator-debug signal)."""
    job_id = uuid4()
    db = _mock_db_for_cancel(
        SimpleNamespace(id=str(job_id), prior_status="running"),
    )
    out = await execution_handler.cancel_active_job(job_id, db)
    assert out["outcome"] == "cancelled"
    assert out["status_before"] == "running"
    assert out["status_after"] == "cancelled"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_cancel_not_found():
    """UPDATE returns no row AND status SELECT returns no row."""
    job_id = uuid4()
    db = _mock_db_for_cancel(updated_rows=None, status_lookup=None)
    out = await execution_handler.cancel_active_job(job_id, db)
    assert out == {"outcome": "not_found", "job_id": str(job_id)}
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_cancel_already_cancelled_is_idempotent():
    """UPDATE returns no row, status SELECT shows job is already cancelled.
    This is the idempotent OK path — handler returns the dedicated
    'already_cancelled' outcome so the router can render a different
    message ('was already cancelled') without re-querying state."""
    job_id = uuid4()
    db = _mock_db_for_cancel(
        updated_rows=None,
        status_lookup=SimpleNamespace(status="cancelled"),
    )
    out = await execution_handler.cancel_active_job(job_id, db)
    assert out == {
        "outcome": "already_cancelled",
        "job_id": str(job_id),
        "status_before": "cancelled",
        "status_after": "cancelled",
    }
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_cancel_wrong_status_completed_returns_409_outcome():
    """A completed job must not be cancelled — terminal state."""
    job_id = uuid4()
    db = _mock_db_for_cancel(
        updated_rows=None,
        status_lookup=SimpleNamespace(status="completed"),
    )
    out = await execution_handler.cancel_active_job(job_id, db)
    assert out == {
        "outcome": "wrong_status",
        "job_id": str(job_id),
        "current_status": "completed",
    }
    db.rollback.assert_awaited_once()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_cancel_wrong_status_failed_returns_409_outcome():
    """A failed job must not be cancelled — terminal state."""
    job_id = uuid4()
    db = _mock_db_for_cancel(
        updated_rows=None,
        status_lookup=SimpleNamespace(status="failed"),
    )
    out = await execution_handler.cancel_active_job(job_id, db)
    assert out["outcome"] == "wrong_status"
    assert out["current_status"] == "failed"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_cancel_update_sql_targets_active_only():
    """The UPDATE must be gated on status NOT IN
    ('completed','failed','cancelled') so two concurrent /cancel callers
    compete safely AND terminal jobs are never mutated. The CTE captures
    prior status for operator-debug. Verify SQL shape + bind params."""
    job_id = uuid4()
    db = _mock_db_for_cancel(
        SimpleNamespace(id=str(job_id), prior_status="executing"),
    )
    await execution_handler.cancel_active_job(job_id, db)

    first_call = db.execute.await_args_list[0]
    sql_obj = first_call.args[0]
    sql_text = str(sql_obj)
    assert "UPDATE jobs" in sql_text
    assert "status = 'cancelled'" in sql_text
    # The status guard — terminal statuses are excluded so we never
    # mutate a completed/failed row and so /cancel-on-cancelled falls
    # through to the idempotent SELECT path.
    assert "NOT IN ('completed','failed','cancelled')" in sql_text
    # CTE captures prior status atomically with the UPDATE.
    assert "WITH prior AS" in sql_text
    assert "FOR UPDATE" in sql_text
    assert "RETURNING jobs.id, prior.prior_status" in sql_text
    assert first_call.args[1] == {"job_id": str(job_id)}


# ---------------------------------------------------------------------------
# Integration — POST /jobs/{job_id}/cancel
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


@pytest.mark.smoke
def test_cancel_endpoint_200_on_successful_flip(client):
    """Happy path: active→cancelled flip returns 200 with cancelled=True
    and was_already_cancelled=False, carrying the prior status."""
    job_id = str(uuid4())
    with patch(
        "app.routers.jobs.cancel_active_job",
        new=AsyncMock(return_value={
            "outcome": "cancelled",
            "job_id": job_id,
            "status_before": "awaiting_confirmation",
            "status_after": "cancelled",
        }),
    ):
        r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == job_id
    assert body["cancelled"] is True
    assert body["was_already_cancelled"] is False
    assert body["status_before"] == "awaiting_confirmation"
    assert body["status_after"] == "cancelled"


@pytest.mark.smoke
def test_cancel_endpoint_200_idempotent_on_already_cancelled(client):
    """Idempotent OK: an already-cancelled job returns 200 with
    was_already_cancelled=True so chat clients can render the no-op
    message without re-querying state."""
    job_id = str(uuid4())
    with patch(
        "app.routers.jobs.cancel_active_job",
        new=AsyncMock(return_value={
            "outcome": "already_cancelled",
            "job_id": job_id,
            "status_before": "cancelled",
            "status_after": "cancelled",
        }),
    ):
        r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["cancelled"] is True
    assert body["was_already_cancelled"] is True


@pytest.mark.smoke
def test_cancel_endpoint_404_on_unknown_job(client):
    """A job_id that doesn't exist surfaces as 404."""
    job_id = str(uuid4())
    with patch(
        "app.routers.jobs.cancel_active_job",
        new=AsyncMock(return_value={
            "outcome": "not_found",
            "job_id": job_id,
        }),
    ):
        r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 404
    assert job_id in r.json()["detail"]


@pytest.mark.smoke
def test_cancel_endpoint_409_on_terminal_status(client):
    """Terminal non-cancellable statuses (completed / failed) return
    409 with current_status in detail. The 409 detail message points
    at /jobs/{id} DELETE for the truly-destructive alternative."""
    job_id = str(uuid4())
    with patch(
        "app.routers.jobs.cancel_active_job",
        new=AsyncMock(return_value={
            "outcome": "wrong_status",
            "job_id": job_id,
            "current_status": "completed",
        }),
    ):
        r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["current_status"] == "completed"
    assert "DELETE" in detail["reason"]


@pytest.mark.smoke
def test_cancel_endpoint_422_on_bad_uuid(client):
    """Malformed UUID → 422; no DB call attempted."""
    with patch(
        "app.routers.jobs.cancel_active_job",
        new=AsyncMock(),
    ) as m:
        r = client.post("/jobs/not-a-uuid/cancel")
    assert r.status_code == 422
    m.assert_not_awaited()
