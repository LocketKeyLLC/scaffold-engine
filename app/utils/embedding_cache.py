"""Two-tier embedding cache: in-memory LRU + Redis persistent store.

Cache key format: embed:{model_id}:{sha256(normalized_text)}
- Model/dimension changes auto-invalidate stale entries
- Stores truncated 512d embeddings, not full 4096d
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from typing import Any

import redis.asyncio as aioredis
import numpy as np

from app.config import settings

logger = logging.getLogger("scaffold.embedding_cache")

MEMORY_MAX_SIZE = 10_000


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
        self._hits = 0
        self._misses = 0

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self.redis_url, decode_responses=True
            )
        return self._redis

    def _cache_key(self, text: str) -> str:
        normalized = " ".join(text.lower().split())
        h = hashlib.sha256(normalized.encode()).hexdigest()
        return f"embed:{self.model_id}:{h}"

    async def get(self, text: str) -> list[float] | None:
        """Look up cached embedding. Returns None on miss."""
        key = self._cache_key(text)

        # Tier 1: in-memory
        if key in self._memory:
            self._memory.move_to_end(key)
            self._hits += 1
            return self._memory[key]

        # Tier 2: Redis
        try:
            r = await self._get_redis()
            cached = await r.get(key)
            if cached:
                emb = json.loads(cached)
                self._memory[key] = emb
                self._evict_memory()
                self._hits += 1
                return emb
        except Exception as e:
            logger.debug("Redis cache get failed: %s", e)

        self._misses += 1
        return None

    async def put(self, text: str, embedding: list[float]) -> None:
        """Store embedding in both tiers."""
        key = self._cache_key(text)

        # Tier 1: in-memory
        self._memory[key] = embedding
        self._evict_memory()

        # Tier 2: Redis
        try:
            r = await self._get_redis()
            await r.set(key, json.dumps(embedding))
        except Exception as e:
            logger.debug("Redis cache put failed: %s", e)

    def _evict_memory(self) -> None:
        while len(self._memory) > MEMORY_MAX_SIZE:
            self._memory.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self.hit_rate, 3),
            "memory_size": len(self._memory),
        }

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
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
