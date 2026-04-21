"""Tests for app/utils/embedding_cache.py (#9.25)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.utils import embedding_cache as ec
from app.utils.embedding_cache import (
    EmbeddingCache,
    _decode_embedding,
    _encode_embedding,
    normalize_cache_text,
)


# ---------------------------------------------------------------------------
# Shared helpers (#130)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_normalize_cache_text_lowercases_and_collapses_whitespace():
    assert normalize_cache_text("  Hello   WORLD  ") == "hello world"


@pytest.mark.smoke
def test_normalize_cache_text_handles_tabs_and_newlines():
    assert normalize_cache_text("A\tB\nC") == "a b c"


# ---------------------------------------------------------------------------
# Binary encoding roundtrip (#45)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_encode_decode_roundtrip_preserves_values():
    emb = [0.1, -0.5, 1e-4, 0.0] * 128  # 512 dims
    back = _decode_embedding(_encode_embedding(emb))
    assert len(back) == 512
    for orig, round_tripped in zip(emb, back):
        assert abs(orig - round_tripped) < 1e-6


@pytest.mark.smoke
def test_encoded_size_is_four_bytes_per_dim():
    emb = [0.0] * 512
    assert len(_encode_embedding(emb)) == 512 * 4


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_cache_key_uses_embedv2_prefix():
    c = EmbeddingCache()
    assert c._cache_key("anything").startswith("embedv2:")


@pytest.mark.smoke
def test_cache_key_is_stable_for_normalized_input():
    c = EmbeddingCache()
    assert c._cache_key("Hello World") == c._cache_key("  hello   world  ")


@pytest.mark.smoke
def test_cache_key_differs_by_model_id():
    c1 = EmbeddingCache(model_id="model-a")
    c2 = EmbeddingCache(model_id="model-b")
    assert c1._cache_key("same text") != c2._cache_key("same text")


# ---------------------------------------------------------------------------
# get / put hit-miss + tiering
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_returns_none_on_total_miss():
    c = EmbeddingCache()
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        result = await c.get("never-seen")
    assert result is None
    assert c._misses == 1
    assert c._hits == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_hits_memory_tier_after_put():
    c = EmbeddingCache()
    emb = [0.1] * 512
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("text", emb)
        result = await c.get("text")
    assert result == emb
    assert c._hits == 1
    # Memory hit should NOT touch Redis
    fake_redis.get.assert_not_called()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_falls_back_to_redis_tier_and_populates_memory():
    c = EmbeddingCache()
    emb = [0.25] * 512
    fake_redis = AsyncMock()
    fake_redis.get.return_value = _encode_embedding(emb)
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        result = await c.get("text")
    assert result == pytest.approx(emb, rel=1e-5)
    assert c._hits == 1
    # Memory tier should now contain it
    key = c._cache_key("text")
    assert key in c._memory


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_put_calls_setex_with_ttl():
    c = EmbeddingCache()
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("text", [0.1] * 512)
    fake_redis.setex.assert_awaited_once()
    args, _ = fake_redis.setex.call_args
    # args = (key, ttl_seconds, blob)
    assert args[0].startswith("embedv2:")
    assert isinstance(args[1], int) and args[1] > 0
    assert isinstance(args[2], bytes)


# ---------------------------------------------------------------------------
# LRU eviction (#128)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_memory_lru_evicts_oldest(monkeypatch):
    monkeypatch.setattr(ec.settings, "embedding_cache_memory_size", 2)
    c = EmbeddingCache()
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("a", [0.1])
        await c.put("b", [0.2])
        await c.put("c", [0.3])  # should evict "a"
    assert len(c._memory) == 2
    assert c._evictions == 1
    assert c._cache_key("a") not in c._memory


# ---------------------------------------------------------------------------
# Redis failure handling (#127)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_redis_failure_counter_resets_on_success():
    c = EmbeddingCache()
    c._redis_failures = 2
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.get("text")
    assert c._redis_failures == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_redis_failure_counter_increments_on_error():
    c = EmbeddingCache()
    fake_redis = AsyncMock()
    fake_redis.get.side_effect = ConnectionError("nope")
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.get("text")
        await c.get("text")
    assert c._redis_failures == 2


# ---------------------------------------------------------------------------
# Stats exposed (#128)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_stats_exposes_counters():
    c = EmbeddingCache()
    c._hits = 3
    c._misses = 7
    c._evictions = 2
    stats = c.stats
    assert stats["hits"] == 3
    assert stats["misses"] == 7
    assert stats["evictions"] == 2
    assert stats["hit_rate"] == pytest.approx(0.3)
