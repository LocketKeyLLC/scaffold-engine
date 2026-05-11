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


# ---------------------------------------------------------------------------
# §17.121 — recheck_upstream
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recheck_one_url_reachable():
    """200 → reachable."""
    from app.modules import research_verify as rv
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    client.head = AsyncMock(return_value=resp)
    with patch.object(rv, "_is_public_host", create=True, return_value=(True, "ok")):
        # _is_public_host is imported INSIDE _recheck_one_url, so patch
        # at the import site instead.
        pass
    with patch("app.modules.research_extractors._is_public_host",
               return_value=(True, "ok")):
        out = await rv._recheck_one_url(client, "https://example.com/a")
    assert out["state"] == "reachable" and out["status"] == 200


@pytest.mark.asyncio
async def test_recheck_one_url_missing_404():
    from app.modules import research_verify as rv
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 404
    client.head = AsyncMock(return_value=resp)
    with patch("app.modules.research_extractors._is_public_host",
               return_value=(True, "ok")):
        out = await rv._recheck_one_url(client, "https://example.com/x")
    assert out["state"] == "missing" and out["status"] == 404


@pytest.mark.asyncio
async def test_recheck_one_url_forbidden_403():
    from app.modules import research_verify as rv
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 403
    client.head = AsyncMock(return_value=resp)
    with patch("app.modules.research_extractors._is_public_host",
               return_value=(True, "ok")):
        out = await rv._recheck_one_url(client, "https://example.com/x")
    assert out["state"] == "forbidden" and out["status"] == 403


@pytest.mark.asyncio
async def test_recheck_one_url_head_405_falls_back_to_get():
    """405 Method Not Allowed → fall back to GET. arxiv.org does this."""
    from app.modules import research_verify as rv
    client = MagicMock()
    head_resp = MagicMock()
    head_resp.status_code = 405
    get_resp = MagicMock()
    get_resp.status_code = 200
    client.head = AsyncMock(return_value=head_resp)
    client.get = AsyncMock(return_value=get_resp)
    with patch("app.modules.research_extractors._is_public_host",
               return_value=(True, "ok")):
        out = await rv._recheck_one_url(client, "https://example.com/x")
    assert out["state"] == "reachable" and out["status"] == 200
    client.get.assert_called_once()


@pytest.mark.asyncio
async def test_recheck_one_url_ssrf_blocked():
    """Private-IP URL → error state, no HTTP call."""
    from app.modules import research_verify as rv
    client = MagicMock()
    client.head = AsyncMock()
    with patch("app.modules.research_extractors._is_public_host",
               return_value=(False, "private_ip")):
        out = await rv._recheck_one_url(client, "http://10.0.0.1/x")
    assert out["state"] == "error" and out["status"] is None
    client.head.assert_not_called()


@pytest.mark.asyncio
async def test_recheck_one_url_empty_url_skipped():
    from app.modules import research_verify as rv
    client = MagicMock()
    out = await rv._recheck_one_url(client, "")
    assert out["state"] == "skipped" and out["status"] is None


@pytest.mark.asyncio
async def test_recheck_one_url_timeout_returns_error():
    from app.modules import research_verify as rv
    import httpx
    client = MagicMock()
    client.head = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    with patch("app.modules.research_extractors._is_public_host",
               return_value=(True, "ok")):
        out = await rv._recheck_one_url(client, "https://example.com/x")
    assert out["state"] == "error" and out["status"] is None


@pytest.mark.asyncio
async def test_verify_session_with_recheck_populates_totals():
    """verify_session(... recheck_upstream=True) calls _recheck_upstream
    and adds the recheck fields to entries + totals."""
    from app.modules import research_verify as rv

    session_id = "44444444-4444-4444-4444-444444444444"
    meta_rows = []
    provenance_rows = [
        {"entry_id": "ok-1", "source_ref": "x", "fetched_at": 1, "quality_signal": {}},
        {"entry_id": "404-1", "source_ref": "y", "fetched_at": 2, "quality_signal": {}},
    ]
    db = _mock_db_session(meta_rows, provenance_rows)

    milvus_rows = {
        "ok-1": {"domain": "eng", "source_type": "tech_docs",
                 "source_url": "https://example.com/ok", "content_hash": "h1",
                 "version": 1, "supersedes_id": None},
        "404-1": {"domain": "eng", "source_type": "tech_docs",
                  "source_url": "https://example.com/gone", "content_hash": "h2",
                  "version": 1, "supersedes_id": None},
    }
    recheck_results = {
        "ok-1": {"state": "reachable", "status": 200},
        "404-1": {"state": "missing", "status": 404},
    }
    with patch.object(rv, "_milvus_lookup_entries", return_value=milvus_rows), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value={}), \
         patch.object(rv, "_recheck_upstream",
                      AsyncMock(return_value=recheck_results)) as rec:
        report = await rv.verify_session(db, session_id, recheck_upstream=True)

    rec.assert_awaited_once()
    assert report["totals"]["reachable"] == 1
    assert report["totals"]["upstream_missing"] == 1
    assert report["totals"]["upstream_error"] == 0
    by_id = {e["entry_id"]: e for e in report["entries"]}
    assert by_id["ok-1"]["upstream_state"] == "reachable"
    assert by_id["ok-1"]["upstream_status"] == 200
    assert by_id["404-1"]["upstream_state"] == "missing"


@pytest.mark.asyncio
async def test_verify_session_without_recheck_omits_recheck_fields():
    """Default recheck_upstream=False leaves recheck fields out — preserves
    backward-compat with pre-§17.121 callers."""
    from app.modules import research_verify as rv

    db = _mock_db_session([], [])
    with patch.object(rv, "_milvus_lookup_entries", return_value={}), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value={}):
        report = await rv.verify_session(db, "55555555-5555-5555-5555-555555555555")
    assert "reachable" not in report["totals"]
    assert "upstream_missing" not in report["totals"]


# ---------------------------------------------------------------------------
# §17.126 — compare_hash mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recheck_one_url_fetch_body_returns_body():
    """When fetch_body=True, GET the URL and include body bytes in result."""
    from app.modules import research_verify as rv
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"hello body"
    client.get = AsyncMock(return_value=resp)
    with patch("app.modules.research_extractors._is_public_host",
               return_value=(True, "ok")):
        out = await rv._recheck_one_url(client, "https://example.com/x", fetch_body=True)
    assert out["state"] == "reachable"
    assert out["status"] == 200
    assert out["body"] == b"hello body"
    client.get.assert_called_once()


@pytest.mark.asyncio
async def test_verify_session_compare_hash_matches():
    """Stored hash == sha256 of re-fetched body → content_state=matches."""
    from app.modules import research_verify as rv
    import hashlib

    body = b"atom xml fixture"
    stored_hash = hashlib.sha256(body).hexdigest()
    session_id = "77777777-7777-7777-7777-777777777777"

    meta_rows = []
    provenance_rows = [
        {"entry_id": "e1", "source_ref": "ref1",
         "fetched_at": 1, "quality_signal": {},
         "raw_upstream_hash": stored_hash},
    ]
    db = _mock_db_session(meta_rows, provenance_rows)

    milvus_rows = {
        "e1": {"domain": "eng", "source_type": "paper_abstract",
               "source_url": "https://arxiv.org/abs/2310.06825",
               "content_hash": "h1", "version": 1, "supersedes_id": None},
    }
    recheck_results = {
        "e1": {"state": "reachable", "status": 200, "body": body},
    }
    with patch.object(rv, "_milvus_lookup_entries", return_value=milvus_rows), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value={}), \
         patch.object(rv, "_recheck_upstream",
                      AsyncMock(return_value=recheck_results)):
        report = await rv.verify_session(
            db, session_id, recheck_upstream=True, compare_hash=True,
        )

    assert report["totals"]["content_matches"] == 1
    assert report["totals"]["content_drifted"] == 0
    e = report["entries"][0]
    assert e["content_state"] == "matches"
    assert e["raw_upstream_hash"] == stored_hash
    assert e["current_upstream_hash"] == stored_hash


@pytest.mark.asyncio
async def test_verify_session_compare_hash_drifted():
    """Stored hash != current → content_state=drifted."""
    from app.modules import research_verify as rv
    import hashlib

    stored_hash = hashlib.sha256(b"original").hexdigest()
    current_body = b"changed body"
    session_id = "88888888-8888-8888-8888-888888888888"

    provenance_rows = [
        {"entry_id": "e1", "source_ref": "ref1",
         "fetched_at": 1, "quality_signal": {},
         "raw_upstream_hash": stored_hash},
    ]
    db = _mock_db_session([], provenance_rows)
    milvus_rows = {
        "e1": {"domain": "eng", "source_type": "paper_abstract",
               "source_url": "https://example.com/x",
               "content_hash": "h1", "version": 1, "supersedes_id": None},
    }
    recheck_results = {"e1": {"state": "reachable", "status": 200, "body": current_body}}
    with patch.object(rv, "_milvus_lookup_entries", return_value=milvus_rows), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value={}), \
         patch.object(rv, "_recheck_upstream",
                      AsyncMock(return_value=recheck_results)):
        report = await rv.verify_session(
            db, session_id, recheck_upstream=True, compare_hash=True,
        )
    assert report["totals"]["content_drifted"] == 1
    assert report["entries"][0]["content_state"] == "drifted"


@pytest.mark.asyncio
async def test_verify_session_compare_hash_unverifiable_no_stored_hash():
    """Entry without raw_upstream_hash → content_state=unverifiable."""
    from app.modules import research_verify as rv

    provenance_rows = [
        {"entry_id": "e1", "source_ref": "ref1",
         "fetched_at": 1, "quality_signal": {},
         "raw_upstream_hash": None},
    ]
    db = _mock_db_session([], provenance_rows)
    milvus_rows = {
        "e1": {"domain": "eng", "source_type": "so_answer",
               "source_url": "https://example.com/x",
               "content_hash": "h1", "version": 1, "supersedes_id": None},
    }
    recheck_results = {"e1": {"state": "reachable", "status": 200, "body": b"x"}}
    with patch.object(rv, "_milvus_lookup_entries", return_value=milvus_rows), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value={}), \
         patch.object(rv, "_recheck_upstream",
                      AsyncMock(return_value=recheck_results)):
        report = await rv.verify_session(
            db, "99999999-9999-9999-9999-999999999999",
            recheck_upstream=True, compare_hash=True,
        )
    assert report["totals"]["content_unverifiable"] == 1
    assert report["entries"][0]["content_state"] == "unverifiable"


@pytest.mark.asyncio
async def test_verify_session_compare_hash_implies_recheck():
    """compare_hash=True forces recheck_upstream=True (hash compare needs body)."""
    from app.modules import research_verify as rv
    db = _mock_db_session([], [])
    with patch.object(rv, "_milvus_lookup_entries", return_value={}), \
         patch.object(rv, "_milvus_lookup_supersedors", return_value={}), \
         patch.object(rv, "_recheck_upstream",
                      AsyncMock(return_value={})) as rec:
        await rv.verify_session(
            db, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            recheck_upstream=False, compare_hash=True,
        )
    rec.assert_awaited_once()
    # Verify fetch_body=True was passed.
    kwargs = rec.call_args.kwargs
    assert kwargs.get("fetch_body") is True


@pytest.mark.smoke
def test_verify_endpoint_accepts_recheck_query_param():
    """Endpoint passes ?recheck=true → verify_session(recheck_upstream=True)."""
    from fastapi.testclient import TestClient
    from app.main import app

    fake_report = {"session_id": "x", "session_meta": None, "totals": {}, "entries": []}
    with patch("app.modules.research_verify.verify_session",
               AsyncMock(return_value=fake_report)) as verify_mock:
        with TestClient(app) as client:
            r = client.get(
                "/research/verify/66666666-6666-6666-6666-666666666666?recheck=true",
                headers=_auth_headers(),
            )
    assert r.status_code == 200
    # Kwarg propagated.
    kwargs = verify_mock.call_args.kwargs
    assert kwargs.get("recheck_upstream") is True


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
