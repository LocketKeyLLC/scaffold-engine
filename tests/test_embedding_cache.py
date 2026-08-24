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
# Cache key — embedv4 (§17.812) with :d{dim}: segment
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_cache_key_uses_embedv4_prefix_and_dim_segment():
    c = EmbeddingCache(dim=512)
    k = c._cache_key("anything")
    assert k.startswith("embedv4:")
    assert ":d512:" in k


@pytest.mark.smoke
def test_cache_key_defaults_to_pipeline_model_not_static_label(monkeypatch):
    """§17.812 (M7) — the key is derived from model_embedder_pipeline (the actual
    vector producer), so swapping it changes the key (old vectors invalidate),
    and it does NOT track the static model_embedder_id label."""
    import app.config

    monkeypatch.setattr(app.config.settings, "model_embedder_pipeline", "model-x")
    monkeypatch.setattr(app.config.settings, "model_embedder_id", "static-label")
    k1 = EmbeddingCache()._cache_key("t")
    assert ":model-x:" in k1
    assert ":static-label:" not in k1

    monkeypatch.setattr(app.config.settings, "model_embedder_pipeline", "model-y")
    k2 = EmbeddingCache()._cache_key("t")
    assert k1 != k2  # pipeline swap → different key → old vectors not served


@pytest.mark.smoke
def test_cache_key_is_stable_for_normalized_input():
    c = EmbeddingCache()
    assert c._cache_key("Hello World") == c._cache_key("  hello   world  ")


@pytest.mark.smoke
def test_cache_key_differs_by_model_id():
    c1 = EmbeddingCache(model_id="model-a")
    c2 = EmbeddingCache(model_id="model-b")
    assert c1._cache_key("same text") != c2._cache_key("same text")


@pytest.mark.smoke
def test_cache_key_differs_by_dim():
    c1 = EmbeddingCache(dim=512)
    c2 = EmbeddingCache(dim=768)
    assert c1._cache_key("same text") != c2._cache_key("same text")


# ---------------------------------------------------------------------------
# get / put hit-miss + tiering
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_returns_none_on_total_miss():
    c = EmbeddingCache(dim=512)
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        result = await c.get("never-seen")
    assert result is None
    assert c._misses == 1
    assert c.hits == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_hits_memory_tier_after_put():
    c = EmbeddingCache(dim=512)
    emb = [0.1] * 512
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("text", emb)
        result = await c.get("text")
    assert result == emb
    assert c._l1_hits == 1
    assert c._l2_hits == 0
    fake_redis.get.assert_not_called()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_falls_back_to_redis_tier_and_populates_memory():
    c = EmbeddingCache(dim=512)
    emb = [0.25] * 512
    fake_redis = AsyncMock()
    fake_redis.get.return_value = _encode_embedding(emb)
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        result = await c.get("text")
    assert result == pytest.approx(emb, rel=1e-5)
    assert c._l1_hits == 0
    assert c._l2_hits == 1
    key = c._cache_key("text")
    assert key in c._memory


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_put_calls_setex_with_ttl():
    c = EmbeddingCache(dim=512)
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("text", [0.1] * 512)
    fake_redis.setex.assert_awaited_once()
    args, _ = fake_redis.setex.call_args
    # args = (key, ttl_seconds, blob)
    assert args[0].startswith("embedv4:")
    assert ":d512:" in args[0]
    assert isinstance(args[1], int) and args[1] > 0
    assert isinstance(args[2], bytes)


# ---------------------------------------------------------------------------
# Dim validation
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_put_rejects_dim_mismatch():
    c = EmbeddingCache(dim=512)
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("text", [0.1] * 10)
    assert c._dim_mismatches == 1
    assert len(c._memory) == 0
    fake_redis.setex.assert_not_awaited()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_get_drops_payload_on_dim_mismatch():
    c = EmbeddingCache(dim=512)
    # Redis returns a 10-dim blob where we expect 512
    bad = _encode_embedding([0.1] * 10)
    fake_redis = AsyncMock()
    fake_redis.get.return_value = bad
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        result = await c.get("text")
    assert result is None
    assert c._misses == 1
    assert c._dim_mismatches == 1
    fake_redis.delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# LRU eviction (#128) — now also covers repeat-put refresh
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_memory_lru_evicts_oldest(monkeypatch):
    monkeypatch.setattr(ec.settings, "embedding_cache_memory_size", 2)
    c = EmbeddingCache(dim=1)
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("a", [0.1])
        await c.put("b", [0.2])
        await c.put("c", [0.3])  # evicts "a"
    assert len(c._memory) == 2
    assert c._evictions == 1
    assert c._cache_key("a") not in c._memory


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_repeat_put_refreshes_lru_position(monkeypatch):
    monkeypatch.setattr(ec.settings, "embedding_cache_memory_size", 2)
    c = EmbeddingCache(dim=1)
    fake_redis = AsyncMock()
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.put("a", [0.1])
        await c.put("b", [0.2])
        await c.put("a", [0.15])  # repeat — refresh "a" to most-recent
        await c.put("c", [0.3])   # should evict "b", not "a"
    assert c._cache_key("a") in c._memory
    assert c._cache_key("b") not in c._memory
    assert c._cache_key("c") in c._memory


# ---------------------------------------------------------------------------
# Redis failure handling (#127)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_redis_failure_counter_resets_on_success():
    c = EmbeddingCache(dim=512)
    c._redis_failures = 2
    fake_redis = AsyncMock()
    fake_redis.get.return_value = None
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.get("text")
    assert c._redis_failures == 0


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_redis_failure_counter_increments_on_error():
    c = EmbeddingCache(dim=512)
    fake_redis = AsyncMock()
    fake_redis.get.side_effect = ConnectionError("nope")
    with patch.object(c, "_get_redis", AsyncMock(return_value=fake_redis)):
        await c.get("text")
        await c.get("text")
    assert c._redis_failures == 2


# ---------------------------------------------------------------------------
# Stats exposed (#128) — L1/L2 split
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_stats_exposes_l1_l2_counters():
    c = EmbeddingCache()
    c._l1_hits = 2
    c._l2_hits = 1
    c._misses = 7
    c._evictions = 2
    c._dim_mismatches = 1
    stats = c.stats
    assert stats["l1_hits"] == 2
    assert stats["l2_hits"] == 1
    assert stats["hits"] == 3
    assert stats["misses"] == 7
    assert stats["evictions"] == 2
    assert stats["dim_mismatches"] == 1
    assert stats["hit_rate"] == pytest.approx(0.3)
