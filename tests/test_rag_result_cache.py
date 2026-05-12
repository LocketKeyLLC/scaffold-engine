"""Tests for app.utils.rag_result_cache — RAG retrieval-result cache."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.utils.rag_result_cache import (
    RagResultCache,
    _KEY_PREFIX,
    _is_cacheable,
    make_key,
)


_OK_RESPONSE = {
    "status": "ok",
    "query": "what is RAG",
    "result_count": 1,
    "results": [{"content": "...", "entry_id": "abc"}],
    "metadata": {"warnings": [], "below_threshold": False, "latency_ms": 42},
}


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestMakeKey:
    def test_basic_shape(self):
        k = make_key("q", "eng", 5, 0.3, False, False, "general")
        assert k.startswith(f"{_KEY_PREFIX}:eng:")
        assert len(k.rsplit(":", 1)[1]) == 64

    def test_none_domain_uses_all_segment(self):
        k = make_key("q", None, 5, 0.3, False, False, "general")
        assert k.startswith(f"{_KEY_PREFIX}:all:")

    def test_deterministic(self):
        a = make_key("q", "eng", 5, 0.3, False, False, "general")
        b = make_key("q", "eng", 5, 0.3, False, False, "general")
        assert a == b

    def test_query_change_changes_key(self):
        a = make_key("q1", "eng", 5, 0.3, False, False, "general")
        b = make_key("q2", "eng", 5, 0.3, False, False, "general")
        assert a != b

    def test_domain_change_changes_key(self):
        a = make_key("q", "eng", 5, 0.3, False, False, "general")
        b = make_key("q", "llm", 5, 0.3, False, False, "general")
        assert a != b

    def test_top_k_change_changes_key(self):
        a = make_key("q", "eng", 5, 0.3, False, False, "general")
        b = make_key("q", "eng", 10, 0.3, False, False, "general")
        assert a != b

    def test_skip_rerank_change_changes_key(self):
        a = make_key("q", "eng", 5, 0.3, False, False, "general")
        b = make_key("q", "eng", 5, 0.3, True, False, "general")
        assert a != b

    def test_include_history_change_changes_key(self):
        a = make_key("q", "eng", 5, 0.3, False, False, "general")
        b = make_key("q", "eng", 5, 0.3, False, True, "general")
        assert a != b

    def test_query_intent_change_changes_key(self):
        a = make_key("q", "eng", 5, 0.3, False, False, "general")
        b = make_key("q", "eng", 5, 0.3, False, False, "code")
        assert a != b

    def test_confidence_change_changes_key(self):
        a = make_key("q", "eng", 5, 0.3, False, False, "general")
        b = make_key("q", "eng", 5, 0.5, False, False, "general")
        assert a != b


# ---------------------------------------------------------------------------
# _is_cacheable
# ---------------------------------------------------------------------------

class TestIsCacheable:
    def test_ok_with_clean_metadata_cacheable(self):
        assert _is_cacheable(_OK_RESPONSE) is True

    def test_error_status_not_cacheable(self):
        assert _is_cacheable({"status": "error", "metadata": {}}) is False

    def test_warnings_blocks_cache(self):
        resp = {**_OK_RESPONSE, "metadata": {"warnings": ["below_threshold"]}}
        assert _is_cacheable(resp) is False

    def test_below_threshold_blocks_cache(self):
        resp = {**_OK_RESPONSE, "metadata": {"warnings": [], "below_threshold": True}}
        assert _is_cacheable(resp) is False

    def test_missing_metadata_treated_as_clean(self):
        # Defensive: a response without metadata still counts as cacheable
        # if status==ok. query_rag always emits metadata, so this is just
        # the empty-warnings fallback.
        assert _is_cacheable({"status": "ok"}) is True


# ---------------------------------------------------------------------------
# RagResultCache — Redis round-trip with mocked client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_miss_returns_none(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    cache._redis = mock_redis

    out = await cache.get("q", "eng", 5, 0.3, False, False, "general")
    assert out is None
    assert cache._misses == 1


@pytest.mark.asyncio
async def test_get_hit_returns_dict(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    payload = json.dumps(_OK_RESPONSE).encode("utf-8")
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=payload)
    cache._redis = mock_redis

    out = await cache.get("q", "eng", 5, 0.3, False, False, "general")
    assert out == _OK_RESPONSE
    assert cache._hits == 1


@pytest.mark.asyncio
async def test_get_corrupt_payload_drops_key(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"not json")
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis

    out = await cache.get("q", "eng", 5, 0.3, False, False, "general")
    assert out is None
    mock_redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_redis_error_fails_open(monkeypatch, caplog):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=ConnectionError("boom"))
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.rag_result_cache"):
        out = await cache.get("q", "eng", 5, 0.3, False, False, "general")
    assert out is None
    assert any("rag_result_cache_get_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_put_ok_writes(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    monkeypatch.setattr("app.utils.rag_result_cache.settings.rag_result_cache_ttl_s", 60)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    cache._redis = mock_redis

    written = await cache.put(
        "q", "eng", 5, 0.3, False, False, "general", _OK_RESPONSE,
    )
    assert written is True
    assert cache._puts == 1
    _, kwargs = mock_redis.set.call_args
    assert kwargs.get("ex") == 60


@pytest.mark.asyncio
async def test_put_error_response_not_cached(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    err = {"status": "error", "error": "boom", "metadata": {}}
    written = await cache.put("q", "eng", 5, 0.3, False, False, "general", err)
    assert written is False
    assert cache._uncacheable == 1
    mock_redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_warnings_response_not_cached(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    resp = {**_OK_RESPONSE, "metadata": {"warnings": ["embed_failed"]}}
    written = await cache.put("q", "eng", 5, 0.3, False, False, "general", resp)
    assert written is False
    assert cache._uncacheable == 1


@pytest.mark.asyncio
async def test_put_below_threshold_response_not_cached(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    resp = {**_OK_RESPONSE, "metadata": {"warnings": [], "below_threshold": True}}
    written = await cache.put("q", "eng", 5, 0.3, False, False, "general", resp)
    assert written is False


@pytest.mark.asyncio
async def test_put_oversized_rejected(monkeypatch, caplog):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    monkeypatch.setattr(
        "app.utils.rag_result_cache.settings.rag_result_cache_max_value_bytes", 100,
    )
    cache = RagResultCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    fat = {**_OK_RESPONSE, "results": [{"content": "x" * 500}]}
    with caplog.at_level("WARNING", logger="scaffold.rag_result_cache"):
        written = await cache.put("q", "eng", 5, 0.3, False, False, "general", fat)
    assert written is False
    assert cache._oversized == 1
    assert any("rag_result_cache_oversized" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_put_redis_error_fails_open(monkeypatch, caplog):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=ConnectionError("boom"))
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.rag_result_cache"):
        written = await cache.put(
            "q", "eng", 5, 0.3, False, False, "general", _OK_RESPONSE,
        )
    assert written is False
    assert any("rag_result_cache_put_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_gate_off_skips_redis(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", False)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    out = await cache.get("q", "eng", 5, 0.3, False, False, "general")
    assert out is None
    assert cache._skipped == 1
    mock_redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_gate_off_is_noop(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", False)
    cache = RagResultCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    written = await cache.put(
        "q", "eng", 5, 0.3, False, False, "general", _OK_RESPONSE,
    )
    assert written is False
    mock_redis.set.assert_not_awaited()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_put_then_get_round_trip(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache.settings.cache_rag_results", True)
    monkeypatch.setattr("app.utils.rag_result_cache.settings.rag_result_cache_ttl_s", 60)
    monkeypatch.setattr(
        "app.utils.rag_result_cache.settings.rag_result_cache_max_value_bytes",
        1024 * 1024,
    )

    store: dict[bytes, bytes] = {}
    mock_redis = AsyncMock()

    async def _set(key, value, ex=None):
        store[key.encode() if isinstance(key, str) else key] = value
    async def _get(key):
        return store.get(key.encode() if isinstance(key, str) else key)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.get = AsyncMock(side_effect=_get)

    cache = RagResultCache()
    cache._redis = mock_redis

    await cache.put("q", "eng", 5, 0.3, False, False, "general", _OK_RESPONSE)
    out = await cache.get("q", "eng", 5, 0.3, False, False, "general")
    assert out == _OK_RESPONSE
    assert cache.stats()["hits"] == 1
    assert cache.stats()["puts"] == 1
