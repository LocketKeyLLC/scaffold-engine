"""§17.138 — tests for EmbeddingCache.warmup.

Verifies that L1 warmup from L2 (Redis) at startup:
  - disables cleanly when knob is 0
  - returns 0 on empty L2
  - loads the expected count up to budget
  - respects embedding_cache_memory_size as a hard cap
  - skips + deletes dim-mismatched / corrupt entries
  - fails soft on every Redis error (SCAN, MGET, DELETE)
  - the SCAN MATCH pattern is scoped to the current model_id + dim
    (won't waste budget on stale-model keys from §17.135 drift)
"""
from __future__ import annotations

import numpy as np
import pytest
from unittest.mock import AsyncMock

from app.utils import embedding_cache as ec


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fake_scan_iter(keys: list[bytes]):
    async def _gen(match=None, count=None):
        for k in keys:
            yield k
    return _gen


def _encode_vec(dim: int, fill: float = 0.1) -> bytes:
    return np.full(dim, fill, dtype=np.float32).tobytes()


def _set_settings(monkeypatch, *, warmup_n: int = 10, memory_size: int = 100):
    monkeypatch.setattr(ec.settings, "embedding_cache_warmup_n", warmup_n)
    monkeypatch.setattr(ec.settings, "embedding_cache_memory_size", memory_size)


# ---------------------------------------------------------------------------
# disabled / empty paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_disabled_by_knob_zero(monkeypatch):
    _set_settings(monkeypatch, warmup_n=0)
    cache = ec.EmbeddingCache()
    mock_redis = AsyncMock()
    # Explicit: scan_iter must NOT be called.
    async def _explode(*a, **kw):
        raise AssertionError("scan_iter must not be called when warmup is disabled")
        yield
    mock_redis.scan_iter = _explode
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out == {"loaded": 0, "skipped": 0, "scanned": 0}
    assert cache._warmup_loaded == 0


@pytest.mark.asyncio
async def test_warmup_disabled_by_memory_size_zero(monkeypatch):
    """If the LRU is sized 0, warmup is a no-op even with a positive N."""
    _set_settings(monkeypatch, warmup_n=10, memory_size=0)
    cache = ec.EmbeddingCache()
    mock_redis = AsyncMock()
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out == {"loaded": 0, "skipped": 0, "scanned": 0}
    mock_redis.scan_iter.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_empty_redis(monkeypatch):
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter([])
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out == {"loaded": 0, "skipped": 0, "scanned": 0}
    # mget should not be called when there are no keys
    mock_redis.mget.assert_not_called()


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_loads_keys_into_l1(monkeypatch):
    _set_settings(monkeypatch, warmup_n=5, memory_size=100)
    cache = ec.EmbeddingCache()
    dim = cache.dim
    keys = [
        f"embedv3:{cache.model_id}:d{dim}:abc{i:02d}".encode()
        for i in range(5)
    ]
    values = [_encode_vec(dim, fill=0.1) for _ in range(5)]

    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(keys)
    mock_redis.mget = AsyncMock(return_value=values)
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out["loaded"] == 5
    assert out["skipped"] == 0
    assert out["scanned"] == 5
    assert len(cache._memory) == 5
    # Every loaded key has a 512-dim vector
    for v in cache._memory.values():
        assert len(v) == dim
    # No stale-delete fired because nothing was stale.
    mock_redis.delete.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_caps_at_memory_size(monkeypatch):
    """warmup_n=50 but memory_size=3 → budget is 3; SCAN stops at 3."""
    _set_settings(monkeypatch, warmup_n=50, memory_size=3)
    cache = ec.EmbeddingCache()
    dim = cache.dim
    # Generate 20 keys but only 3 should be SCAN'd before we break.
    keys_seen: list[bytes] = []

    async def _scan(match=None, count=None):
        for i in range(20):
            k = f"embedv3:{cache.model_id}:d{dim}:k{i:02d}".encode()
            keys_seen.append(k)
            yield k

    mock_redis = AsyncMock()
    mock_redis.scan_iter = _scan
    mock_redis.mget = AsyncMock(side_effect=lambda keys: [_encode_vec(dim)] * len(keys))
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out["loaded"] == 3
    assert len(cache._memory) == 3
    # The early-break must have happened after exactly 3 yields.
    assert len(keys_seen) == 3


# ---------------------------------------------------------------------------
# dim-mismatched / corrupt keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_skips_and_deletes_dim_mismatched(monkeypatch):
    """A stale key with wrong dim is counted as skipped + deleted server-side."""
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()
    dim = cache.dim
    keys = [
        f"embedv3:{cache.model_id}:d{dim}:ok0".encode(),
        f"embedv3:{cache.model_id}:d{dim}:bad".encode(),
        f"embedv3:{cache.model_id}:d{dim}:ok1".encode(),
    ]
    values = [
        _encode_vec(dim, 0.1),
        # wrong dim — 256 instead of 512
        np.full(256, 0.2, dtype=np.float32).tobytes(),
        _encode_vec(dim, 0.3),
    ]
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(keys)
    mock_redis.mget = AsyncMock(return_value=values)
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out["loaded"] == 2
    assert out["skipped"] == 1
    assert out["scanned"] == 3
    # The bad key was deleted server-side
    mock_redis.delete.assert_awaited_once()
    deleted_keys = mock_redis.delete.await_args.args
    assert keys[1] in deleted_keys


@pytest.mark.asyncio
async def test_warmup_missing_value_not_skipped(monkeypatch):
    """A key returned by SCAN but MGET-empty (TTL race) is silently skipped
    — neither loaded nor counted as a skip+delete (it's already gone)."""
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()
    dim = cache.dim
    keys = [
        f"embedv3:{cache.model_id}:d{dim}:ok".encode(),
        f"embedv3:{cache.model_id}:d{dim}:gone".encode(),
    ]
    values = [_encode_vec(dim, 0.1), None]
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(keys)
    mock_redis.mget = AsyncMock(return_value=values)
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out["loaded"] == 1
    assert out["skipped"] == 0  # missing-value is not a skip
    assert out["scanned"] == 2
    # No delete because nothing was decoded-and-failed.
    mock_redis.delete.assert_not_called()


# ---------------------------------------------------------------------------
# fail-soft on every Redis error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_scan_failure_returns_zero(monkeypatch, caplog):
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()

    async def _failing_scan(match=None, count=None):
        raise ConnectionError("redis fell over")
        yield
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _failing_scan
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.embedding_cache"):
        out = await cache.warmup()
    assert out == {"loaded": 0, "skipped": 0, "scanned": 0}
    assert any(
        "embedding_cache_warmup_scan_failed" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_warmup_mget_failure_returns_partial(monkeypatch, caplog):
    """SCAN succeeded with N keys but MGET raised → return scanned=N
    + loaded=0 (no partial pollution of L1)."""
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()
    dim = cache.dim
    keys = [f"embedv3:{cache.model_id}:d{dim}:k{i}".encode() for i in range(3)]
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(keys)
    mock_redis.mget = AsyncMock(side_effect=ConnectionError("redis fell over"))
    cache._redis = mock_redis

    with caplog.at_level("WARNING", logger="scaffold.embedding_cache"):
        out = await cache.warmup()
    assert out == {"loaded": 0, "skipped": 0, "scanned": 3}
    assert any(
        "embedding_cache_warmup_mget_failed" in r.getMessage()
        for r in caplog.records
    )
    # L1 still empty
    assert len(cache._memory) == 0


@pytest.mark.asyncio
async def test_warmup_stale_delete_failure_does_not_lose_loaded(monkeypatch):
    """If the dim-mismatch cleanup DELETE fails, the loaded count is
    still correct — L1 isn't rolled back."""
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()
    dim = cache.dim
    keys = [
        f"embedv3:{cache.model_id}:d{dim}:ok".encode(),
        f"embedv3:{cache.model_id}:d{dim}:bad".encode(),
    ]
    values = [
        _encode_vec(dim, 0.1),
        np.full(256, 0.2, dtype=np.float32).tobytes(),  # wrong dim
    ]
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(keys)
    mock_redis.mget = AsyncMock(return_value=values)
    mock_redis.delete = AsyncMock(side_effect=ConnectionError("redis fell over"))
    cache._redis = mock_redis

    out = await cache.warmup()
    assert out["loaded"] == 1
    assert out["skipped"] == 1
    assert len(cache._memory) == 1


# ---------------------------------------------------------------------------
# SCAN pattern scoping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_scan_pattern_scoped_to_current_identity(monkeypatch):
    """SCAN MATCH must include the current model_id + dim so stale-model
    keys (e.g. left over from a §17.135 embedder swap) aren't scanned."""
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()
    seen_match: list[str] = []

    async def _scan(match=None, count=None):
        seen_match.append(match)
        for _ in range(0):
            yield  # empty
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _scan
    cache._redis = mock_redis

    await cache.warmup()
    assert len(seen_match) == 1
    pattern = seen_match[0]
    assert pattern.startswith("embedv3:")
    assert f":{cache.model_id}:" in pattern
    assert f":d{cache.dim}:" in pattern
    assert pattern.endswith(":*")


# ---------------------------------------------------------------------------
# stats integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_stats_surface_loaded_and_skipped(monkeypatch):
    _set_settings(monkeypatch, warmup_n=5)
    cache = ec.EmbeddingCache()
    dim = cache.dim
    keys = [
        f"embedv3:{cache.model_id}:d{dim}:ok".encode(),
        f"embedv3:{cache.model_id}:d{dim}:bad".encode(),
    ]
    values = [
        _encode_vec(dim, 0.1),
        np.full(64, 0.2, dtype=np.float32).tobytes(),  # wrong dim
    ]
    mock_redis = AsyncMock()
    mock_redis.scan_iter = _fake_scan_iter(keys)
    mock_redis.mget = AsyncMock(return_value=values)
    mock_redis.delete = AsyncMock()
    cache._redis = mock_redis

    await cache.warmup()
    stats = cache.stats
    assert stats["warmup_loaded"] == 1
    assert stats["warmup_skipped"] == 1
    # Pre-existing fields still present
    assert "hits" in stats and "misses" in stats and "evictions" in stats
