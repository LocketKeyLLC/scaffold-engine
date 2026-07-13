"""§17.189 — runtime drift checks against ``app.modules._rag_protocol``.

The TypedDicts in ``_rag_protocol.py`` document the contract that
``query_rag`` / ``ingest_entries`` return. Python doesn't runtime-check
TypedDict, so the tests below provide the actual drift guard:

  * Build a synthetic response that mirrors the success-path shape and
    validate it against ``validate_rag_response`` — confirms the validator
    accepts what the code returns.
  * Build deliberately-broken variants and confirm the validator flags them
    (missing top-level key, missing per-result key, missing per-scores key,
    missing ``query`` on a status='ok' response).
  * For ``ingest_entries``, run the live function path with mocks and
    validate the actual return dict — this is the load-bearing test that
    fires if the implementation ever adds/removes a stat field without the
    matching TypedDict update.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules._rag_protocol import (
    _INGEST_STATS_REQUIRED_KEYS,
    _RAG_RESPONSE_REQUIRED_KEYS,
    _RAG_RESULT_REQUIRED_KEYS,
    _RAG_SCORES_REQUIRED_KEYS,
    validate_ingest_stats,
    validate_rag_response,
)


# ---------------------------------------------------------------------------
# Fixtures — sample shapes that mirror the production code paths.
# ---------------------------------------------------------------------------

def _sample_result() -> dict:
    return {
        "content": "C", "title": "T", "tags": "", "source_url": "https://x",
        "entry_id": "e1", "domain": "eng", "version": 1, "supersedes_id": "",
        "confidence_score": 0.9, "source_type": "tech_docs", "provenance": None,
        "scores": {
            "vector": 0.5, "keyword": 0.0, "rrf": 0.1,
            "rerank": 0.8, "final": 0.85, "quality_bump": 1.0,
        },
    }


def _sample_ok_response() -> dict:
    return {
        "status": "ok", "query": "q", "result_count": 1,
        "results": [_sample_result()],
        "metadata": {
            "vector_hits": 1, "keyword_hits": 0, "fused_count": 1,
            "confidence_threshold": 0.8, "threshold_relaxed": False,
            "below_threshold": False, "fell_back_to_top3": False,
            "reranked": True, "skipped_rerank": False,
            "reranker_backend": "CrossEncoder", "warnings": [],
            "latency_ms": 12.3,
        },
    }


def _sample_error_response() -> dict:
    return {
        "status": "error", "error": "collection_unavailable",
        "results": [],
        "metadata": {"warnings": ["collection_unavailable"], "reranker_backend": None},
    }


# ---------------------------------------------------------------------------
# validate_rag_response — happy paths
# ---------------------------------------------------------------------------

def test_validate_rag_response_accepts_ok_shape():
    assert validate_rag_response(_sample_ok_response()) == []


def test_validate_rag_response_accepts_error_shape():
    """The error path omits query / result_count — validator should not
    require them when status != 'ok'."""
    assert validate_rag_response(_sample_error_response()) == []


def test_validate_rag_response_accepts_empty_results_on_ok():
    """A successful query with zero matches still passes — list[] is fine."""
    resp = _sample_ok_response()
    resp["results"] = []
    resp["result_count"] = 0
    assert validate_rag_response(resp) == []


# ---------------------------------------------------------------------------
# validate_rag_response — drift detection
# ---------------------------------------------------------------------------

def test_validate_rag_response_flags_missing_top_level_key():
    resp = _sample_ok_response()
    del resp["metadata"]
    errors = validate_rag_response(resp)
    assert any("missing top-level keys" in e and "metadata" in e for e in errors)


def test_validate_rag_response_flags_missing_query_when_ok():
    resp = _sample_ok_response()
    del resp["query"]
    errors = validate_rag_response(resp)
    assert any("missing 'query'" in e for e in errors)


def test_validate_rag_response_flags_missing_result_key():
    resp = _sample_ok_response()
    del resp["results"][0]["entry_id"]
    errors = validate_rag_response(resp)
    assert any("results[0] missing keys" in e and "entry_id" in e for e in errors)


def test_validate_rag_response_flags_missing_scores_key():
    resp = _sample_ok_response()
    del resp["results"][0]["scores"]["rerank"]
    errors = validate_rag_response(resp)
    assert any("results[0].scores missing keys" in e and "rerank" in e for e in errors)


# ---------------------------------------------------------------------------
# Required-keys snapshots — these locks future TypedDict edits
# ---------------------------------------------------------------------------

def test_required_keys_snapshot_rag_response():
    """Snapshot the required-keys frozenset. A future TypedDict edit that
    adds or removes a field will fire this test, forcing the author to
    update both the TypedDict and the consumer-side caller code."""
    assert _RAG_RESPONSE_REQUIRED_KEYS == frozenset({
        "status", "results", "metadata",
    })


def test_required_keys_snapshot_rag_result():
    assert _RAG_RESULT_REQUIRED_KEYS == frozenset({
        "content", "title", "tags", "source_url", "entry_id", "domain",
        "version", "supersedes_id", "confidence_score", "source_type",
        "provenance", "scores",
    })


def test_required_keys_snapshot_scores():
    assert _RAG_SCORES_REQUIRED_KEYS == frozenset({
        "vector", "keyword", "rrf", "rerank", "final", "quality_bump",
    })


def test_required_keys_snapshot_ingest_stats():
    assert _INGEST_STATS_REQUIRED_KEYS == frozenset({
        "new", "versioned", "rejected", "skipped_hash", "skipped_empty",
    })


# ---------------------------------------------------------------------------
# validate_ingest_stats
# ---------------------------------------------------------------------------

def test_validate_ingest_stats_accepts_valid_shape():
    stats = {"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 2, "skipped_empty": 0}
    assert validate_ingest_stats(stats) == []


def test_validate_ingest_stats_flags_missing_key():
    stats = {"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0}  # missing skipped_empty
    errors = validate_ingest_stats(stats)
    assert any("missing keys" in e and "skipped_empty" in e for e in errors)


def test_validate_ingest_stats_flags_wrong_type():
    stats = {"new": "1", "versioned": 0, "rejected": 0, "skipped_hash": 0, "skipped_empty": 0}
    errors = validate_ingest_stats(stats)
    assert any("new: expected int, got str" in e for e in errors)


# ---------------------------------------------------------------------------
# Live drift guards — fire if the implementation diverges from the typed shape
# ---------------------------------------------------------------------------

async def test_ingest_entries_empty_input_matches_typed_shape():
    """The ``not entries`` early-return branch must still emit a complete
    IngestStatsDict — a future short-cut that returns ``{}`` would silently
    break callers that pattern-match the field set."""
    from app.modules.rag_pipeline import ingest_entries
    stats = await ingest_entries([], domain="eng")
    assert validate_ingest_stats(stats) == []


async def test_query_rag_error_path_matches_typed_shape(monkeypatch):
    """Hit the ``collection unavailable`` branch in query_rag and verify the
    error response still satisfies RagResponseDict."""
    from app.modules import rag_pipeline as rp
    from app.utils.rag_result_cache import RagResultCache

    async def _miss(*a, **kw):
        return None

    fake_cache = MagicMock()
    fake_cache.get = AsyncMock(return_value=None)
    fake_cache.put = AsyncMock()

    with patch.object(rp, "get_rag_result_cache", return_value=fake_cache), \
         patch.object(rp, "_get_client", return_value=None):
        resp = await rp.query_rag("q", domain="eng")
    assert resp["status"] == "error"
    # The error path omits query / result_count — validator must accept that.
    assert validate_rag_response(resp) == []
