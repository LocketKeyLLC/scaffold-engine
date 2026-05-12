"""Tests for the §17.133 fetch-cache cardinality cap."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.utils.fetch_cache import FetchCache, _KEY_PREFIX


def _fake_scan_iter(keys: list[bytes]):
    """Return a side_effect-friendly async iterator that yields the given keys."""
    async def _gen(match=None, count=None):
        for k in keys:
            yield k
    return _gen


# ---------------------------------------------------------------------------
# _key_count
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_key_count_returns_scan_total(monkeypatch):
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(
        [f"{_KEY_PREFIX}:gh:abc:{i:016x}".encode() for i in range(7)]
    )
    cache._redis = mock_redis

    count = await cache._key_count()
    assert count == 7
    assert cache._last_count == 7
    assert cache._last_count_ts > 0


@pytest.mark.asyncio
async def test_key_count_within_interval_uses_cached_value(monkeypatch):
    """Two consecutive calls within the interval must SCAN only once."""
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()
    scan_calls = 0

    async def _scan(match=None, count=None):
        nonlocal scan_calls
        scan_calls += 1
        for k in [b"fetchv1:gh:abc:0000000000000001",
                  b"fetchv1:gh:abc:0000000000000002"]:
            yield k
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _scan
    cache._redis = mock_redis

    a = await cache._key_count()
    b = await cache._key_count()
    assert a == b == 2
    assert scan_calls == 1


@pytest.mark.asyncio
async def test_key_count_force_bypasses_interval(monkeypatch):
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()
    scan_calls = 0

    async def _scan(match=None, count=None):
        nonlocal scan_calls
        scan_calls += 1
        for k in [b"fetchv1:gh:abc:0000000000000001"]:
            yield k
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _scan
    cache._redis = mock_redis

    await cache._key_count()
    await cache._key_count(force=True)
    assert scan_calls == 2


@pytest.mark.asyncio
async def test_key_count_scan_failure_returns_minus_one(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.utils.fetch_cache.settings.fetch_cache_count_interval_s", 30,
    )
    cache = FetchCache()

    async def _scan(match=None, count=None):
        raise ConnectionError("redis is sleeping")
        yield  # unreachable; needed for async-generator type
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _scan
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
    mock_redis.scan_iter = _fake_scan_iter(
        [b"fetchv1:gh:abc:0000000000000001"] * 3,
    )
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
    mock_redis.scan_iter = _fake_scan_iter(
        [b"fetchv1:gh:abc:0000000000000001",
         b"fetchv1:gh:abc:0000000000000002",
         b"fetchv1:gh:abc:0000000000000003"],
    )
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
    mock_redis.scan_iter = _fake_scan_iter(
        [b"fetchv1:gh:abc:0000000000000001"] * 10,
    )
    cache._redis = mock_redis

    ok = await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    assert ok is False
    assert cache._capped == 1


@pytest.mark.asyncio
async def test_put_cap_disabled_writes(monkeypatch):
    """max_keys=0 disables the check; no SCAN, no capping."""
    _set_caps(monkeypatch, max_keys=0)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    # If the check ran, _key_count would call scan_iter. Wire it to fail
    # so we'd notice the wrong path.
    async def _explode(*a, **kw):
        raise AssertionError("scan_iter must not be called when max_keys=0")
        yield  # unreachable
    mock_redis.scan_iter = _explode
    cache._redis = mock_redis

    ok = await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    assert ok is True
    mock_redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_scan_failure_fails_open(monkeypatch):
    """A Redis hiccup on SCAN must NOT block puts — a hiccup ≠ breach,
    and blocking would be strictly worse than the breach itself."""
    _set_caps(monkeypatch, max_keys=10)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    async def _failing_scan(match=None, count=None):
        raise ConnectionError("transient")
        yield  # unreachable
    mock_redis.scan_iter = _failing_scan
    cache._redis = mock_redis

    ok = await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    assert ok is True
    assert cache._capped == 0
    mock_redis.set.assert_awaited_once()


@pytest.mark.asyncio
async def test_put_count_cached_across_puts(monkeypatch):
    """Five puts in rapid succession should run SCAN exactly once."""
    _set_caps(monkeypatch, max_keys=100, count_interval_s=30)
    cache = FetchCache()
    scan_calls = 0

    async def _scan(match=None, count=None):
        nonlocal scan_calls
        scan_calls += 1
        for k in [b"fetchv1:gh:abc:0000000000000001"]:
            yield k
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.scan_iter = _scan
    cache._redis = mock_redis

    for i in range(5):
        await cache.put("gh", "abc", f"p{i}", b"hi", ttl_seconds=60)
    assert scan_calls == 1
    assert mock_redis.set.await_count == 5


@pytest.mark.asyncio
async def test_stats_exposes_capped_and_last_count(monkeypatch):
    _set_caps(monkeypatch, max_keys=2)
    cache = FetchCache()
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(
        [b"fetchv1:gh:abc:0000000000000001",
         b"fetchv1:gh:abc:0000000000000002"],
    )
    cache._redis = mock_redis

    await cache.put("gh", "abc", "x", b"hi", ttl_seconds=60)
    stats = cache.stats()
    assert stats["capped"] == 1
    assert stats["last_count"] == 2
    # Pre-existing fields still present
    assert "hits" in stats and "misses" in stats and "puts" in stats
    assert "oversized" in stats


# ---------------------------------------------------------------------------
# Regression — body-size and TTL checks still fire before cardinality
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oversized_rejected_before_cardinality(monkeypatch):
    """A body that fails the size cap should bail without consulting
    the cardinality count — keeps oversized stat distinct from capped."""
    _set_caps(monkeypatch, max_keys=10, max_body=10)
    cache = FetchCache()
    mock_redis = AsyncMock()
    # SCAN must NOT be called on the oversized path.
    async def _explode(*a, **kw):
        raise AssertionError("scan_iter must not be called on oversized path")
        yield
    mock_redis.scan_iter = _explode
    mock_redis.set = AsyncMock()
    cache._redis = mock_redis

    ok = await cache.put("gh", "abc", "x", b"x" * 50, ttl_seconds=60)
    assert ok is False
    assert cache._oversized == 1
    assert cache._capped == 0
    mock_redis.set.assert_not_awaited()
