"""Wiring tests for the RAG result cache inside query_rag.

The cache itself is exercised in test_rag_result_cache.py; this module
verifies that query_rag (a) short-circuits the embed → search → rerank
pipeline on a cache hit, (b) puts on a clean ok response, (c) does not
put on error / warnings, and (d) bypasses cache entirely when the gate
is off.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

# Reuse the shared patch builder from the main rag_pipeline test module.
from tests.test_rag_pipeline import _PatchStack, _patch_rag_deps, _run


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(
        "app.utils.rag_result_cache.settings.cache_rag_results", True,
    )
    monkeypatch.setattr(
        "app.utils.rag_result_cache.settings.rag_result_cache_ttl_s", 60,
    )
    monkeypatch.setattr(
        "app.utils.rag_result_cache.settings.rag_result_cache_max_value_bytes",
        1024 * 1024,
    )


@pytest.fixture
def fresh_cache_singleton(monkeypatch):
    monkeypatch.setattr("app.utils.rag_result_cache._cache", None)


@pytest.fixture
def fake_redis(monkeypatch, fresh_cache_singleton):
    """In-memory fake Redis bound to the rag-result-cache singleton."""
    store: dict[bytes, bytes] = {}
    mock_redis = AsyncMock()

    async def _set(key, value, ex=None):
        k = key.encode() if isinstance(key, str) else key
        store[k] = value
    async def _get(key):
        k = key.encode() if isinstance(key, str) else key
        return store.get(k)
    async def _delete(key):
        k = key.encode() if isinstance(key, str) else key
        store.pop(k, None)
    mock_redis.set = AsyncMock(side_effect=_set)
    mock_redis.get = AsyncMock(side_effect=_get)
    mock_redis.delete = AsyncMock(side_effect=_delete)

    from app.utils.rag_result_cache import get_rag_result_cache
    cache = get_rag_result_cache()
    cache._redis = mock_redis
    cache._hits = 0
    cache._misses = 0
    cache._puts = 0
    cache._skipped = 0
    cache._uncacheable = 0
    cache._oversized = 0
    return cache


def test_second_identical_call_hits_cache(gate_on, fake_redis):
    """First call runs pipeline + writes; second call skips embed/search/rerank."""
    patches = _patch_rag_deps()
    with _PatchStack(patches):
        from app.modules.rag_pipeline import query_rag
        first = _run(query_rag(
            "test query", domain="eng", confidence_threshold=0.0,
        ))
        second = _run(query_rag(
            "test query", domain="eng", confidence_threshold=0.0,
        ))

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    # Same results
    assert first["results"] == second["results"]
    # Cache-hit marker present on the second
    assert second["metadata"].get("cache_hit") is True
    # First call was not a cache hit
    assert "cache_hit" not in first["metadata"]
    # Pipeline ran exactly once
    assert patches["_embed_query"].await_count == 1
    assert patches["_vector_search"].await_count == 1
    assert patches["_keyword_search"].await_count == 1
    assert patches["_rerank"].await_count == 1
    assert fake_redis.stats()["hits"] == 1
    assert fake_redis.stats()["puts"] == 1


def test_different_domain_misses(gate_on, fake_redis):
    """Different domain → different cache key → full pipeline runs again."""
    patches = _patch_rag_deps()
    with _PatchStack(patches):
        from app.modules.rag_pipeline import query_rag
        _run(query_rag("q", domain="eng", confidence_threshold=0.0))
        _run(query_rag("q", domain="llm", confidence_threshold=0.0))

    assert patches["_embed_query"].await_count == 2
    assert fake_redis.stats()["puts"] == 2


def test_error_response_not_cached(gate_on, fake_redis):
    """A status=error response (collection unavailable) is not stored."""
    patches = _patch_rag_deps(collection_ok=False)
    with _PatchStack(patches):
        from app.modules.rag_pipeline import query_rag
        out = _run(query_rag("q", domain="eng", confidence_threshold=0.0))

    assert out["status"] == "error"
    # Not cached → second call still hits the (mocked) pipeline.
    assert fake_redis.stats()["puts"] == 0
    assert fake_redis.stats()["uncacheable"] == 0  # we never call put on error


def test_gate_off_skips_cache_entirely(monkeypatch, fresh_cache_singleton):
    """With cache_rag_results=False, every call runs the pipeline; Redis untouched."""
    monkeypatch.setattr(
        "app.utils.rag_result_cache.settings.cache_rag_results", False,
    )
    mock_redis = AsyncMock()
    from app.utils.rag_result_cache import get_rag_result_cache
    cache = get_rag_result_cache()
    cache._redis = mock_redis

    patches = _patch_rag_deps()
    with _PatchStack(patches):
        from app.modules.rag_pipeline import query_rag
        _run(query_rag("q", domain="eng", confidence_threshold=0.0))
        _run(query_rag("q", domain="eng", confidence_threshold=0.0))

    assert patches["_embed_query"].await_count == 2
    mock_redis.get.assert_not_awaited()
    mock_redis.set.assert_not_awaited()


def test_cache_failure_falls_through_to_pipeline(monkeypatch, gate_on, fresh_cache_singleton):
    """If Redis errors on get, query_rag must still return a fresh result."""
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    mock_redis.set = AsyncMock(side_effect=ConnectionError("redis down"))

    from app.utils.rag_result_cache import get_rag_result_cache
    cache = get_rag_result_cache()
    cache._redis = mock_redis

    patches = _patch_rag_deps()
    with _PatchStack(patches):
        from app.modules.rag_pipeline import query_rag
        out = _run(query_rag("q", domain="eng", confidence_threshold=0.0))

    assert out["status"] == "ok"
    # Pipeline ran (cache miss → real pipeline)
    assert patches["_embed_query"].await_count == 1
