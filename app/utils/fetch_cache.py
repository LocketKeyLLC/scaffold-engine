"""Upstream HTTP-body cache for deep-search producers.

Key format: ``fetchv1:{source_type}:{ref}:{path_hash}``

- ``source_type``: producer slug (gh, hf, so, hn, arxiv, reddit, wiki).
  Allowlisted; anything else is rejected at construction.
- ``ref``: immutable ref (SHA, HF revision, post ID) for long-TTL
  cache-forever semantics, or a time-bucket ("latest-2026-05-10") for
  short-TTL semantics.
- ``path_hash``: SHA256(path)[:16]. Paths can be long URLs or repo
  paths; hashing keeps Redis keys bounded and printable.

Bodies are raw bytes; producers handle decoding. Bodies exceeding
``settings.fetch_cache_max_body_bytes`` are dropped silently with a
WARNING log — a single huge response can't blow out Redis.
"""
from __future__ import annotations

import hashlib
import logging
import re

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("scaffold.fetch_cache")

_KEY_PREFIX = "fetchv1"

ALLOWED_SOURCE_TYPES: frozenset[str] = frozenset({
    "gh", "hf", "so", "hn", "arxiv", "reddit", "wiki",
})

# ref must be filesystem-safe + readable: alnum, -._/, plus colon for
# scheme-qualified refs like "tag:v1.2.3". Length-capped to keep keys printable.
_REF_RE = re.compile(r"^[A-Za-z0-9._\-/:]{1,128}$")


def _hash_path(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def make_key(source_type: str, ref: str, path: str) -> str:
    """Build a fetch-cache key. Raises ``ValueError`` on invalid input."""
    if source_type not in ALLOWED_SOURCE_TYPES:
        raise ValueError(
            f"source_type={source_type!r} not in ALLOWED_SOURCE_TYPES "
            f"({sorted(ALLOWED_SOURCE_TYPES)})"
        )
    if not _REF_RE.match(ref):
        raise ValueError(f"ref={ref!r} fails {_REF_RE.pattern}")
    if not path:
        raise ValueError("path must be non-empty")
    return f"{_KEY_PREFIX}:{source_type}:{ref}:{_hash_path(path)}"


class FetchCache:
    """Async Redis-backed body cache for upstream HTTP fetches.

    No L1 in-memory tier — bodies are KB–MB scale; the embedding cache's
    LRU pattern doesn't pay off here.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.redis_url
        self._redis: aioredis.Redis | None = None
        self._hits = 0
        self._misses = 0
        self._puts = 0
        self._oversized = 0

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self.redis_url, decode_responses=False)
        return self._redis

    async def get(self, source_type: str, ref: str, path: str) -> bytes | None:
        """Return cached body or None on miss. Never raises on Redis error."""
        try:
            key = make_key(source_type, ref, path)
        except ValueError as e:
            logger.warning("fetch_cache_bad_key: %s", e)
            return None
        try:
            r = await self._get_redis()
            body = await r.get(key)
        except Exception as e:
            logger.warning("fetch_cache_get_failed: key=%s err=%s", key, e)
            return None
        if body:
            self._hits += 1
            return bytes(body)
        self._misses += 1
        return None

    async def put(
        self,
        source_type: str,
        ref: str,
        path: str,
        body: bytes,
        ttl_seconds: int,
    ) -> bool:
        """Store body. Returns True on write, False on rejection/error.

        Rejects empty bodies, oversized bodies, and non-positive TTLs.
        """
        try:
            key = make_key(source_type, ref, path)
        except ValueError as e:
            logger.warning("fetch_cache_bad_key: %s", e)
            return False
        if not body:
            return False
        max_bytes = settings.fetch_cache_max_body_bytes
        if len(body) > max_bytes:
            self._oversized += 1
            logger.warning(
                "fetch_cache_oversized: key=%s bytes=%d cap=%d",
                key, len(body), max_bytes,
            )
            return False
        if ttl_seconds <= 0:
            logger.warning("fetch_cache_invalid_ttl: %d", ttl_seconds)
            return False
        try:
            r = await self._get_redis()
            await r.set(key, body, ex=ttl_seconds)
        except Exception as e:
            logger.warning("fetch_cache_put_failed: key=%s err=%s", key, e)
            return False
        self._puts += 1
        return True

    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "puts": self._puts,
            "oversized": self._oversized,
        }


_cache: FetchCache | None = None


def get_fetch_cache() -> FetchCache:
    """Module-level singleton accessor."""
    global _cache
    if _cache is None:
        _cache = FetchCache()
    return _cache
