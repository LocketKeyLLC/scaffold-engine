"""Tests for §17.114 — /research/verify/<session_id> endpoint + helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# verify_session — unit tests against mocked DB + Milvus
# ---------------------------------------------------------------------------

class _FakeMappingsResult:
    def __init__(self, rows):
        self._rows = rows
    def first(self):
        return self._rows[0] if self._rows else None
    def __iter__(self):
        return iter(self._rows)


def _mock_db_session(meta_rows, provenance_rows):
    """Build an AsyncMock DB session.

    `meta_rows` is what the first SELECT (session metadata) returns;
    `provenance_rows` is what get_provenance_for_session's SELECT returns.
    The function makes TWO execute calls in order, so we side_effect a list.
    """
    sess = AsyncMock()
    meta_result = MagicMock()
    meta_result.mappings.return_value = _FakeMappingsResult(meta_rows)
    prov_result = MagicMock()
    prov_result.mappings.return_value = _FakeMappingsResult(provenance_rows)
    sess.execute = AsyncMock(side_effect=[meta_result, prov_result])
    return sess


@pytest.mark.asyncio
async def test_verify_session_classifies_present_superseded_missing():
    from app.modules import research_verify as rv

    session_id = "11111111-1111-1111-1111-111111111111"

    meta_rows = [{
        "topic": "transformer architecture",
        "status": "completed",
        "completed_at": datetime(2026, 5, 10, tzinfo=timezone.utc),
    }]
    provenance_rows = [
        {"entry_id": "e-present", "source_ref": "abc123",
         "fetched_at": 1700000000, "quality_signal": {"x": 1}},
        {"entry_id": "e-superseded", "source_ref": "def456",
         "fetched_at": 1700000100, "quality_signal": {}},
        {"entry_id": "e-missing", "source_ref": "ghi789",
         "fetched_at": 1700000200, "quality_signal": {}},
    ]
    db = _mock_db_session(meta_rows, provenance_rows)

    # Milvus lookup: 2 of 3 present (e-missing is not returned)
    milvus_rows = {
        "e-present": {
            "domain": "eng", "source_type": "model_card",
            "source_url": "https://huggingface.co/owner/repo",
            "content_hash": "h1", "version": 1, "supersedes_id": None,
        },
        "e-superseded": {
            "domain": "eng", "source_type": "so_answer",
            "source_url": "https://stackoverflow.com/a/42",
            "content_hash": "h2", "version": 1, "supersedes_id": None,
        },
    }
    # e-superseded has a newer entry pointing at it.
    supersedors = {"e-superseded": "e-superseded-v2"}

    with patch.object(rv, "_milvus_lookup_entries", return_value=milvus_rows), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value=supersedors):
        report = await rv.verify_session(db, session_id)

    assert report["session_id"] == session_id
    assert report["session_meta"]["topic"] == "transformer architecture"
    assert report["session_meta"]["status"] == "completed"
    assert report["totals"] == {
        "provenance_rows": 3, "in_milvus": 1, "superseded": 1, "missing": 1,
    }

    by_id = {e["entry_id"]: e for e in report["entries"]}
    assert by_id["e-present"]["milvus_state"] == "present"
    assert by_id["e-present"]["source_type"] == "model_card"
    assert by_id["e-superseded"]["milvus_state"] == "superseded"
    assert by_id["e-superseded"]["superseded_by"] == "e-superseded-v2"
    assert by_id["e-missing"]["milvus_state"] == "missing"
    assert by_id["e-missing"]["in_milvus"] is False


@pytest.mark.asyncio
async def test_verify_session_unknown_returns_empty_entries():
    """Pre-§17.114 sessions (or wrong UUID) → no provenance rows.

    Verify still returns a well-formed report so callers can render it.
    """
    from app.modules import research_verify as rv

    session_id = "22222222-2222-2222-2222-222222222222"
    db = _mock_db_session(meta_rows=[], provenance_rows=[])

    with patch.object(rv, "_milvus_lookup_entries", return_value={}), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value={}):
        report = await rv.verify_session(db, session_id)

    assert report["session_id"] == session_id
    assert report["session_meta"] is None
    assert report["entries"] == []
    assert report["totals"]["provenance_rows"] == 0


# ---------------------------------------------------------------------------
# /research/verify/{session_id} — endpoint behavior
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    from app.config import settings
    return {"X-API-Key": settings.scaffold_api_key.get_secret_value()}


@pytest.mark.smoke
def test_verify_endpoint_rejects_non_uuid():
    """Endpoint validates session_id is a UUID; non-UUID → 400."""
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/research/verify/not-a-uuid", headers=_auth_headers())
    assert r.status_code == 400
    assert "Invalid session_id" in r.json()["detail"]


@pytest.mark.smoke
def test_verify_endpoint_calls_verify_session():
    """Endpoint dispatches to verify_session and returns its JSON shape."""
    from fastapi.testclient import TestClient
    from app.main import app

    fake_report = {
        "session_id": "33333333-3333-3333-3333-333333333333",
        "session_meta": None,
        "totals": {"provenance_rows": 0, "in_milvus": 0, "superseded": 0, "missing": 0},
        "entries": [],
    }
    with patch("app.modules.research_verify.verify_session",
               AsyncMock(return_value=fake_report)) as verify_mock:
        with TestClient(app) as client:
            r = client.get(
                f"/research/verify/{fake_report['session_id']}",
                headers=_auth_headers(),
            )
    assert r.status_code == 200
    assert r.json() == fake_report
    verify_mock.assert_awaited_once()
