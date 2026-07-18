"""Tests for the §17.133 fetch-cache cardinality cap.

§17.620 (audit #32) — the count is now an exact O(1) ``DBSIZE`` against the
fetch cache's own logical Redis DB (only fetch keys live there) instead of a
``SCAN MATCH fetchv1:*`` over the shared keyspace. These tests mock ``dbsize``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.utils.fetch_cache import FetchCache


# ---------------------------------------------------------------------------
# _key_count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_key_count_returns_dbsize(monkeypatch):
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.dbsize = AsyncMock(return_value=7)
    cache._redis = mock_redis

    count = await cache._key_count()
    assert count == 7
    assert cache._last_count == 7
    assert cache._last_count_ts > 0


@pytest.mark.asyncio
async def test_key_count_within_interval_uses_cached_value(monkeypatch):
    """Two consecutive calls within the interval must query DBSIZE only once."""
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.dbsize = AsyncMock(return_value=2)
    cache._redis = mock_redis

    a = await cache._key_count()
    b = await cache._key_count()
    assert a == b == 2
    assert mock_redis.dbsize.await_count == 1


@pytest.mark.asyncio
async def test_key_count_force_bypasses_interval(monkeypatch):
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.dbsize = AsyncMock(return_value=1)
    cache._redis = mock_redis

    await cache._key_count()
    await cache._key_count(force=True)
    assert mock_redis.dbsize.await_count == 2


@pytest.mark.asyncio
async def test_key_count_dbsize_failure_returns_minus_one(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.dbsize = AsyncMock(side_effect=ConnectionError("redis is sleeping"))
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.fetch_cache"):
        out = await cache._key_count()
    assert out == -1
    assert any("fetch_cache_count_failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# put — cardinality cap
# ---------------------------------------------------------------------------

def _set_caps(monkeypatch, *, max_keys: int = 5, max_body: int = 1024,
              ttl_default: int = 60, ttl_immutable: int = 86400,
              count_interval_s: int = 30):
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_max_keys", max_keys,
    )
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_max_body_bytes", max_body,
    )
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s",
        count_interval_s,
    )


@pytest.mark.asyncio
async def test_put_below_cap_writes(monkeypatch):
    _set_caps(monkeypatch, max_keys=10)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.dbsize = AsyncMock(return_value=3)
    cache._redis = mock_redis

    ok = await cache.put("gh", "abcdef", "README.md", b"hi", ttl_seconds=60)
    assert ok is True
    assert cache._capped == 0
    mock_redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_at_cap_rejected(monkeypatch, caplog):
    """A put hitting the cap returns False and increments _capped without
    touching Redis (no set() call)."""
    _set_caps(monkeypatch, max_keys=3)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.dbsize = AsyncMock(return_value=3)
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.fetch_cache"):
        ok = await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    assert ok is False
    assert cache._capped == 1
    mock_redis.set.assert_not_awaited()
    assert any(
        "fetch_cache_cardinality_capped" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_put_above_cap_rejected(monkeypatch):
    """Counts beyond the cap (Redis grew between samples) still reject."""
    _set_caps(monkeypatch, max_keys=2)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.dbsize = AsyncMock(return_value=10)
    cache._redis = mock_redis

    ok = await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    assert ok is False
    assert cache._capped == 1


@pytest.mark.asyncio
async def test_put_cap_disabled_writes(monkeypatch):
    """max_keys=0 disables the check; no DBSIZE, no capping."""
    _set_caps(monkeypatch, max_keys=0)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    # If the check ran, _key_count would call dbsize. Wire it to fail so we'd
    # notice the wrong path.
    mock_redis.dbsize = AsyncMock(
        side_effect=AssertionError("dbsize must not be called when max_keys=0")
    )
    cache._redis = mock_redis

    ok = await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    assert ok is True
    mock_redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_count_failure_fails_open(monkeypatch):
    """A Redis hiccup on the count must NOT block puts — a hiccup ≠ breach,
    and blocking would be strictly worse than the breach itself."""
    _set_caps(monkeypatch, max_keys=10)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.dbsize = AsyncMock(side_effect=ConnectionError("transient"))
    cache._redis = mock_redis

    ok = await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    assert ok is True
    assert cache._capped == 0
    mock_redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_count_cached_across_puts(monkeypatch):
    """Five puts in rapid succession should query DBSIZE exactly once."""
    _set_caps(monkeypatch, max_keys=100, count_interval_s=30)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.dbsize = AsyncMock(return_value=1)
    cache._redis = mock_redis

    for i in range(5):
        await cache.put("gh", "abc", f"p{i}", b"hi", ttl_seconds=60)
    assert mock_redis.dbsize.await_count == 1
