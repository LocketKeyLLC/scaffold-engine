"""Tests for app.utils.fetch_cache — key construction + Redis round-trip."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from app.utils.fetch_cache import (
    ALLOWED_SOURCE_TYPES,
    FetchCache,
    _KEY_PREFIX,
    make_key,
)


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestMakeKey:
    def test_basic_shape(self):
        k = make_key("gh", "abc123def", "README.md")
        assert k.startswith(f"{_KEY_PREFIX}:gh:abc123def:")
        assert len(k.rsplit(":", 1)[1]) == 16  # path SHA256[:16]

    def test_deterministic(self):
        assert make_key("gh", "v1", "docs/api.md") == make_key("gh", "v1", "docs/api.md")

    def test_path_change_changes_key(self):
        assert make_key("gh", "v1", "a") != make_key("gh", "v1", "b")

    def test_ref_change_changes_key(self):
        assert make_key("gh", "v1", "a") != make_key("gh", "v2", "a")

    def test_source_type_change_changes_key(self):
        assert make_key("gh", "v1", "a") != make_key("hf", "v1", "a")

    def test_unknown_source_type_rejected(self):
        with pytest.raises(ValueError, match="not in ALLOWED_SOURCE_TYPES"):
            make_key("twitter", "abc", "foo")

    @pytest.mark.parametrize(
        "bad_ref",
        ["", " ", "abc def", "abc;DROP", "a" * 129, "evil\nref"],
    )
    def test_invalid_ref_rejected(self, bad_ref):
        with pytest.raises(ValueError, match="fails"):
            make_key("gh", bad_ref, "foo")

    def test_empty_path_rejected(self):
        with pytest.raises(ValueError, match="path must be non-empty"):
            make_key("gh", "v1", "")

    @pytest.mark.parametrize("source_type", sorted(ALLOWED_SOURCE_TYPES))
    def test_all_allowed_source_types(self, source_type):
        k = make_key(source_type, "v1", "x")
        assert f":{source_type}:" in k

    def test_real_world_refs(self):
        # git tag, HF revision, SO post-id, arXiv id — all must pass.
        for ref in ["v2.32.0", "release/main", "abc123def4567890",
                    "12345678", "2310.06825"]:
            make_key("gh", ref, "x")


# ---------------------------------------------------------------------------
# FetchCache — Redis round-trip with mocked client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_miss_returns_none():
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)
    cache._redis = mock_redis

    body = await cache.get("gh", "v1", "README.md")
    assert body is None
    assert cache._misses == 1
    assert cache._hits == 0


@pytest.mark.asyncio
async def test_get_hit_returns_bytes():
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=b"# README")
    cache._redis = mock_redis

    body = await cache.get("gh", "v1", "README.md")
    assert body == b"# README"
    assert cache._hits == 1


@pytest.mark.asyncio
async def test_get_redis_error_returns_none():
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    cache._redis = mock_redis

    body = await cache.get("gh", "v1", "README.md")
    assert body is None


@pytest.mark.asyncio
async def test_put_then_get_round_trip():
    cache = FetchCache()
    mock_redis = AsyncMock()
    storage: dict[str, bytes] = {}

    async def fake_set(key, value, ex=None):
        storage[key] = value
        return True

    async def fake_get(key):
        return storage.get(key)

    mock_redis.set = AsyncMock(side_effect=fake_set)
    mock_redis.get = AsyncMock(side_effect=fake_get)
    cache._redis = mock_redis

    assert await cache.put("gh", "v1", "README.md", b"# README", ttl_seconds=3600) is True
    assert await cache.get("gh", "v1", "README.md") == b"# README"
    assert cache._puts == 1
    assert cache._hits == 1


@pytest.mark.asyncio
async def test_put_oversized_dropped(caplog):
    from app.config import settings
    cache = FetchCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    oversized = b"x" * (settings.fetch_cache_max_body_bytes + 1)
    with caplog.at_level("WARNING", logger="scaffold.fetch_cache"):
        ok = await cache.put("gh", "v1", "big", oversized, ttl_seconds=60)
    assert ok is False
    assert cache._oversized == 1
    assert any("fetch_cache_oversized" in r.getMessage() for r in caplog.records)
    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_put_empty_body_skipped():
    cache = FetchCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    assert await cache.put("gh", "v1", "empty", b"", ttl_seconds=60) is False
    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_put_invalid_ttl_rejected():
    cache = FetchCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    assert await cache.put("gh", "v1", "x", b"data", ttl_seconds=0) is False
    assert await cache.put("gh", "v1", "x", b"data", ttl_seconds=-1) is False
    mock_redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_put_redis_error_returns_false():
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
    cache._redis = mock_redis

    assert await cache.put("gh", "v1", "x", b"data", ttl_seconds=60) is False


@pytest.mark.asyncio
async def test_get_bad_key_returns_none():
    cache = FetchCache()
    assert await cache.get("not_allowed", "v1", "x") is None


@pytest.mark.asyncio
async def test_put_bad_key_returns_false():
    cache = FetchCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis
    assert await cache.put("not_allowed", "v1", "x", b"data", ttl_seconds=60) is False
    mock_redis.set.assert_not_called()


def test_stats_counters_start_at_zero():
    cache = FetchCache()
    assert cache.stats() == {"hits": 0, "misses": 0, "puts": 0, "oversized": 0}
