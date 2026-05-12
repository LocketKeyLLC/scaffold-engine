"""
Integration tests for /specs/* — exercises the real Postgres ``specs``
table through the FastAPI router so we catch any drift between
``app/sim/spec_store.py`` SQL, the response Pydantic shapes, and the
schema persisted by migration 040.

Each test inserts its own spec row directly via SQL (rather than
through the extractor) to keep the test independent of LLM
availability. A teardown helper deletes whatever the test created.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.routers.specs import CONFIRMED_BY_API_KEY

ORCHESTRATOR_URL = "http://scaffold-orchestrator:8000"

# A minimal but valid spec — matches spec_schema.json. Re-validating
# is the schema test's job; here we only need a row that exists.
_VALID_SPEC: dict = {
    "schema_version": "1.0.0",
    "design": {
        "name": "RC LPF (integration)",
        "kind": "analog_circuit",
        "description": "Inserted by test_specs_router_db.",
    },
    "constraints": [
        {
            "id": "fc_3db",
            "kind": "electrical.frequency",
            "description": "Corner.",
            "target": 1000.0,
            "tolerance_pct": 5.0,
            "unit": "Hz",
            "criticality": "required",
        }
    ],
}


@pytest_asyncio.fixture
async def inserted_spec():
    """Insert one spec; track its id so the test can clean up."""
    inserted_ids: list[str] = []

    async def _insert(*, confirmed: bool = False) -> str:
        async with async_session() as db:
            row = await db.execute(
                text(
                    """
                    INSERT INTO specs (
                        schema_version, spec_json, spec_sha256,
                        confirmed_by, confirmed_at
                    )
                    VALUES (
                        :sv, CAST(:sj AS JSONB), :sh,
                        :cb,
                        CASE WHEN :is_conf THEN NOW() ELSE NULL END
                    )
                    RETURNING id
                    """
                ),
                {
                    "sv": _VALID_SPEC["schema_version"],
                    "sj": json.dumps(_VALID_SPEC),
                    "sh": f"test-{uuid.uuid4().hex[:16]}",
                    "cb": "preexisting" if confirmed else None,
                    "is_conf": confirmed,
                },
            )
            spec_id = str(row.scalar_one())
            await db.commit()
        inserted_ids.append(spec_id)
        return spec_id

    yield _insert

    if inserted_ids:
        async with async_session() as db:
            await db.execute(
                text("DELETE FROM specs WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": inserted_ids},
            )
            await db.commit()


def _api_headers() -> dict[str, str]:
    """Mirror the orchestrator's auth posture — read the same env var
    the container itself runs with so the test passes whether auth is
    enabled or disabled."""
    raw = settings.scaffold_api_key.get_secret_value()
    return {"X-API-Key": raw} if raw else {}


@pytest.mark.smoke
async def test_confirm_endpoint_sets_columns(inserted_spec):
    spec_id = await inserted_spec(confirmed=False)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/specs/{spec_id}/confirm",
            headers=_api_headers(),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == spec_id
    assert body["confirmed_by"] == CONFIRMED_BY_API_KEY
    assert body["confirmed_at"] is not None

    # Read back via SQL to confirm the row was actually updated.
    async with async_session() as db:
        row = await db.execute(
            text(
                "SELECT confirmed_by, confirmed_at FROM specs WHERE id = :id"
            ),
            {"id": spec_id},
        )
        persisted = row.mappings().one()
    assert persisted["confirmed_by"] == CONFIRMED_BY_API_KEY
    assert persisted["confirmed_at"] is not None


@pytest.mark.smoke
async def test_confirm_is_idempotent_reconfirm_updates_timestamp(inserted_spec):
    spec_id = await inserted_spec(confirmed=True)

    async with async_session() as db:
        row = await db.execute(
            text("SELECT confirmed_at FROM specs WHERE id = :id"),
            {"id": spec_id},
        )
        t0: datetime = row.scalar_one()

    # Sleep is overkill (NOW() has microsecond resolution); just hit
    # the endpoint and assert the new timestamp is >= the prior one.
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/specs/{spec_id}/confirm",
            headers=_api_headers(),
        )
    assert resp.status_code == 200

    async with async_session() as db:
        row = await db.execute(
            text("SELECT confirmed_at, confirmed_by FROM specs WHERE id = :id"),
            {"id": spec_id},
        )
        persisted = row.mappings().one()
    assert persisted["confirmed_at"] >= t0
    # confirmed_by is rewritten to the API-key principal, regardless
    # of what it was before.
    assert persisted["confirmed_by"] == CONFIRMED_BY_API_KEY


@pytest.mark.smoke
async def test_unconfirm_endpoint_clears_columns(inserted_spec):
    spec_id = await inserted_spec(confirmed=True)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/specs/{spec_id}/unconfirm",
            headers=_api_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed_by"] is None
    assert body["confirmed_at"] is None

    async with async_session() as db:
        row = await db.execute(
            text(
                "SELECT confirmed_by, confirmed_at FROM specs WHERE id = :id"
            ),
            {"id": spec_id},
        )
        persisted = row.mappings().one()
    assert persisted["confirmed_by"] is None
    assert persisted["confirmed_at"] is None


@pytest.mark.smoke
async def test_confirm_404_when_spec_missing():
    bogus = uuid.uuid4()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/specs/{bogus}/confirm",
            headers=_api_headers(),
        )
    assert resp.status_code == 404
    assert "not found" in resp.json().get("detail", "").lower()


@pytest.mark.smoke
async def test_unconfirm_404_when_spec_missing():
    bogus = uuid.uuid4()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/specs/{bogus}/unconfirm",
            headers=_api_headers(),
        )
    assert resp.status_code == 404


@pytest.mark.smoke
async def test_pending_list_returns_unconfirmed_rows(inserted_spec):
    pending_id = await inserted_spec(confirmed=False)
    confirmed_id = await inserted_spec(confirmed=True)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/specs/pending",
            headers=_api_headers(),
        )
    assert resp.status_code == 200
    body = resp.json()
    ids = {item["id"] for item in body["pending"]}
    assert pending_id in ids
    assert confirmed_id not in ids
    assert body["count"] == len(body["pending"])
