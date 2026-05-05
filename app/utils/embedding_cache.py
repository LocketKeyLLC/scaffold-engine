"""Two-tier embedding cache: in-memory LRU + Redis persistent store.

Cache key format: embedv3:{model_id}:d{dim}:{sha256(normalized_text)}
- v3 folds embedding_dim into the key so dim changes auto-invalidate (#45 follow-up).
- Old embedv2 keys expire naturally via Redis TTL.
- Stores truncated 512d embeddings, not full 4096d.
- Binary float32 encoding: ~4x smaller than JSON.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from typing import Any

import redis.asyncio as aioredis
import numpy as np

from app.config import settings

logger = logging.getLogger("scaffold.embedding_cache")

# Consecutive Redis failures before escalating log level debug -> warning
_REDIS_FAILURE_THRESHOLD = 3

# Cache key version prefix (bump on wire-format or key-schema change)
_KEY_PREFIX = "embedv3"


def normalize_cache_text(text: str) -> str:
    """Lowercase + whitespace-collapse text for cache-key hashing (#130)."""
    return " ".join(text.lower().split())


def _encode_embedding(embedding: list[float]) -> bytes:
    """Pack embedding as float32 bytes."""
    return np.asarray(embedding, dtype=np.float32).tobytes()


def _decode_embedding(blob: bytes) -> list[float]:
    """Unpack float32 bytes back to list[float]. Length unvalidated."""
    return np.frombuffer(blob, dtype=np.float32).tolist()


class EmbeddingCache:
    """Two-tier async embedding cache."""

    def __init__(
        self,
        redis_url: str | None = None,
        model_id: str | None = None,
        dim: int | None = None,
    ):
        self.redis_url = redis_url or settings.redis_url
        self.model_id = model_id or settings.model_embedder_id
        self.dim = dim or settings.embedding_dim
        self._memory: OrderedDict[str, list[float]] = OrderedDict()
        self._redis: aioredis.Redis | None = None
        self._redis_lock = asyncio.Lock()
        self._l1_hits = 0
        self._l2_hits = 0
        self._misses = 0
        self._evictions = 0
        self._redis_failures = 0
        self._dim_mismatches = 0
        # Surface dim+model_id at construction so a mismatch with stored
        # keys produces a one-line diagnostic in startup logs rather than
        # silent miss-storms after a config change.
        logger.info(
            "embedding_cache: init key_prefix=%s model_id=%s dim=%d "
            "(keys not matching this dim will read as miss + invalidate)",
            _KEY_PREFIX, self.model_id, self.dim,
        )

    async def _get_redis(self) -> aioredis.Redis:
        # Fast path
        if self._redis is not None:
            return self._redis
        # Slow path — serialize concurrent first-callers
        async with self._redis_lock:
            if self._redis is None:
                self._redis = aioredis.from_url(
                    self.redis_url, decode_responses=False
                )
        return self._redis

    def _cache_key(self, text: str) -> str:
        h = hashlib.sha256(normalize_cache_text(text).encode()).hexdigest()
        return f"{_KEY_PREFIX}:{self.model_id}:d{self.dim}:{h}"

    def _decode_validated(self, blob: bytes, key: str) -> list[float] | None:
        """Decode + length check. Returns None on mismatch and logs."""
        vec = _decode_embedding(blob)
        if len(vec) != self.dim:
            self._dim_mismatches += 1
            logger.warning(
                "embedding_cache: dim mismatch on decode (key=%s expected=%d got=%d)",
                key, self.dim, len(vec),
            )
            return None
        return vec

    async def get(self, text: str) -> list[float] | None:
        """Look up cached embedding. Returns None on miss or dim mismatch."""
        key = self._cache_key(text)

        # Tier 1: in-memory
        if key in self._memory:
            self._memory.move_to_end(key)
            self._l1_hits += 1
            return self._memory[key]

        # Tier 2: Redis
        try:
            r = await self._get_redis()
            cached = await r.get(key)
            self._redis_failures = 0
            if cached:
                emb = self._decode_validated(cached, key)
                if emb is None:
                    # Stale/corrupt payload — treat as miss, drop the key
                    try:
                        await r.delete(key)
                    except Exception:
                        pass
                    self._misses += 1
                    return None
                self._memory[key] = emb
                self._evict_memory()
                self._l2_hits += 1
                return emb
        except Exception as e:
            self._redis_failures += 1
            _log = logger.warning if self._redis_failures >= _REDIS_FAILURE_THRESHOLD else logger.debug
            _log("Redis cache get failed (consecutive=%d): %s", self._redis_failures, e)

        self._misses += 1
        return None

    async def put(self, text: str, embedding: list[float]) -> None:
        """Store embedding in both tiers. Rejects on dim mismatch."""
        if len(embedding) != self.dim:
            self._dim_mismatches += 1
            logger.warning(
                "embedding_cache: dim mismatch on put (expected=%d got=%d) — dropping",
                self.dim, len(embedding),
            )
            return

        key = self._cache_key(text)

        # Tier 1: in-memory (refresh LRU position on repeat puts)
        if key in self._memory:
            self._memory.move_to_end(key)
            self._memory[key] = embedding
        else:
            self._memory[key] = embedding
            self._evict_memory()

        # Tier 2: Redis
        try:
            r = await self._get_redis()
            await r.setex(key, settings.embedding_cache_ttl_s, _encode_embedding(embedding))
            self._redis_failures = 0
        except Exception as e:
            self._redis_failures += 1
            _log = logger.warning if self._redis_failures >= _REDIS_FAILURE_THRESHOLD else logger.debug
            _log("Redis cache put failed (consecutive=%d): %s", self._redis_failures, e)

    def _evict_memory(self) -> None:
        while len(self._memory) > settings.embedding_cache_memory_size:
            self._memory.popitem(last=False)
            self._evictions += 1

    @property
    def hits(self) -> int:
        return self._l1_hits + self._l2_hits

    @property
    def hit_rate(self) -> float:
        total = self.hits + self._misses
        return self.hits / total if total > 0 else 0.0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "l1_hits": self._l1_hits,
            "l2_hits": self._l2_hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 3),
            "memory_size": len(self._memory),
            "evictions": self._evictions,
            "dim_mismatches": self._dim_mismatches,
        }

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None


def truncate_and_normalize(
    embedding: list[float], dim: int | None = None
) -> list[float]:
    """MRL truncation: slice to first `dim` dimensions, L2 normalize."""
    dim = dim or settings.embedding_dim
    vec = np.array(embedding[:dim], dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


# Module-level singleton
_cache: EmbeddingCache | None = None


def get_cache() -> EmbeddingCache:
    global _cache
    if _cache is None:
        _cache = EmbeddingCache()
    return _cache
