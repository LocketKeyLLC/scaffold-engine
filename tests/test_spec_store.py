"""
Unit tests for ``app.sim.spec_store`` — confirmation-gate helpers.

DB is mocked via ``make_mock_db`` from conftest. We assert the exact
SQL parameters bound for each operation so a future schema change
fails loudly rather than silently writing the wrong column.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.sim.spec_store import (
    SpecNotConfirmedError,
    SpecNotFoundError,
    confirm_spec,
    get_spec,
    is_spec_confirmed,
    list_pending_confirmations,
    require_confirmed_spec,
    unconfirm_spec,
)
from tests.conftest import make_mock_db


def _row(*, confirmed: bool = False) -> dict:
    """Build a mappings-row matching the SELECT/RETURNING clauses."""
    return {
        "id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "schema_version": "1.0.0",
        "spec_json": {
            "design": {"name": "RC LPF", "kind": "analog_circuit", "description": "."},
            "constraints": [],
            "schema_version": "1.0.0",
        },
        "spec_sha256": "abc123",
        "confirmed_by": "api_key" if confirmed else None,
        "confirmed_at": datetime.now(timezone.utc) if confirmed else None,
        "created_at": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# get_spec
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_get_spec_returns_row():
    row = _row()
    db = make_mock_db([row])
    out = await get_spec(db, row["id"])
    assert out.id == row["id"]
    assert out.schema_version == "1.0.0"
    assert out.is_confirmed is False


@pytest.mark.smoke
async def test_get_spec_raises_when_absent():
    db = make_mock_db([])
    with pytest.raises(SpecNotFoundError):
        await get_spec(db, uuid.uuid4())


# ---------------------------------------------------------------------------
# confirm_spec / unconfirm_spec
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_confirm_spec_sets_columns_and_commits():
    row = _row(confirmed=True)
    db = make_mock_db([row])
    out = await confirm_spec(db, row["id"], confirmed_by="api_key")

    assert out.is_confirmed is True
    assert out.confirmed_by == "api_key"
    # SQL params for the UPDATE include the confirmed_by literal.
    update_call = db.execute.await_args
    assert update_call.args[1]["confirmed_by"] == "api_key"
    assert update_call.args[1]["id"] == str(row["id"])
    assert db.commit.await_count == 1


@pytest.mark.smoke
async def test_confirm_spec_raises_when_id_unknown():
    db = make_mock_db([])
    with pytest.raises(SpecNotFoundError):
        await confirm_spec(db, uuid.uuid4(), confirmed_by="api_key")
    # No commit on the missing path.
    assert db.commit.await_count == 0


@pytest.mark.smoke
async def test_unconfirm_spec_clears_columns():
    row = _row(confirmed=False)  # post-update row reflects NULL
    db = make_mock_db([row])
    out = await unconfirm_spec(db, row["id"])
    assert out.is_confirmed is False
    assert db.commit.await_count == 1


@pytest.mark.smoke
async def test_unconfirm_spec_raises_when_id_unknown():
    db = make_mock_db([])
    with pytest.raises(SpecNotFoundError):
        await unconfirm_spec(db, uuid.uuid4())


# ---------------------------------------------------------------------------
# is_spec_confirmed
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_is_spec_confirmed_true_when_columns_set():
    db = make_mock_db(scalar=True)
    assert await is_spec_confirmed(db, uuid.uuid4()) is True


@pytest.mark.smoke
async def test_is_spec_confirmed_false_when_columns_null():
    db = make_mock_db(scalar=False)
    assert await is_spec_confirmed(db, uuid.uuid4()) is False


@pytest.mark.smoke
async def test_is_spec_confirmed_false_when_missing():
    db = make_mock_db(scalar=None)
    assert await is_spec_confirmed(db, uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# require_confirmed_spec — the strict gate
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_require_confirmed_spec_returns_row_when_confirmed():
    row = _row(confirmed=True)
    db = make_mock_db([row])
    out = await require_confirmed_spec(db, row["id"])
    assert out.is_confirmed is True


@pytest.mark.smoke
async def test_require_confirmed_spec_raises_spec_not_confirmed():
    row = _row(confirmed=False)
    db = make_mock_db([row])
    with pytest.raises(SpecNotConfirmedError) as exc_info:
        await require_confirmed_spec(db, row["id"])
    assert exc_info.value.spec_id == row["id"]


@pytest.mark.smoke
async def test_require_confirmed_spec_raises_spec_not_found():
    db = make_mock_db([])
    with pytest.raises(SpecNotFoundError):
        await require_confirmed_spec(db, uuid.uuid4())


# ---------------------------------------------------------------------------
# list_pending_confirmations
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_list_pending_returns_unconfirmed_rows():
    rows = [_row(confirmed=False) for _ in range(3)]
    db = make_mock_db(rows)
    out = await list_pending_confirmations(db)
    assert len(out) == 3
    assert all(not r.is_confirmed for r in out)


@pytest.mark.smoke
async def test_list_pending_scopes_to_job_id():
    job_id = uuid.uuid4()
    db = make_mock_db([])
    await list_pending_confirmations(db, job_id=job_id)
    # SQL params include the job_id filter.
    call = db.execute.await_args
    assert call.args[1]["job_id"] == str(job_id)


@pytest.mark.smoke
async def test_list_pending_honors_limit():
    db = make_mock_db([])
    await list_pending_confirmations(db, limit=5)
    call = db.execute.await_args
    assert call.args[1]["lim"] == 5
