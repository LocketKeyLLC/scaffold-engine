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

import asyncio
import hashlib
import logging
import re
import time

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
        # §17.133 — cardinality cap. _last_count is the most recent SCAN
        # result for fetchv1:* keys; _last_count_ts is its monotonic
        # capture time. Concurrent puts serialize on _count_lock so only
        # one SCAN runs per refresh interval. _capped tracks puts
        # rejected by the cap (distinct from _oversized, which counts
        # by-body-size rejections).
        self._last_count = 0
        self._last_count_ts = 0.0
        self._capped = 0
        self._count_lock = asyncio.Lock()

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            # §17.620 (audit #32) — bind to the fetch cache's own logical DB so
            # the cardinality count can be an O(1) DBSIZE (only fetch keys live
            # there) instead of a SCAN over the shared keyspace. NOTE: a `db=`
            # kwarg to from_url does NOT win — ConnectionPool.from_url lets the
            # URL's db path override kwargs — so we rewrite the URL's db path.
            from urllib.parse import urlsplit, urlunsplit
            parts = urlsplit(self.redis_url)
            url = urlunsplit(parts._replace(path=f"/{settings.fetch_cache_redis_db}"))
            self._redis = aioredis.from_url(url, decode_responses=False)
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

    async def _key_count(self, force: bool = False) -> int:
        """Return the count of fetch keys in Redis.

        §17.620 (audit #32) — an exact O(1) ``DBSIZE`` against the fetch
        cache's dedicated logical DB (``fetch_cache_redis_db``), where fetch
        bodies are the ONLY keys, rather than a ``SCAN MATCH fetchv1:*`` that
        walked the entire shared 2GB allkeys-lru keyspace on every refresh.

        The result is still cached for ``fetch_cache_count_interval_s`` seconds
        (DBSIZE is cheap, but the throttle bounds round-trips under a put
        burst). Pass ``force=True`` to bypass the time cache.

        Returns ``-1`` on Redis failure — callers must treat that as
        "unknown" and NOT block puts on a hiccup (a hiccup is not a
        cardinality breach; blocking would be strictly worse).
        """
        now = time.monotonic()
        interval = settings.fetch_cache_count_interval_s
        if not force and (now - self._last_count_ts) < interval:
            return self._last_count
        async with self._count_lock:
            # Re-check under the lock — a peer may have just refreshed.
            now = time.monotonic()
            if not force and (now - self._last_count_ts) < interval:
                return self._last_count
            try:
                r = await self._get_redis()
                count = int(await r.dbsize())
                self._last_count = count
                self._last_count_ts = now
                return count
            except Exception as e:
                logger.warning("fetch_cache_count_failed: err=%s", e)
                return -1

    async def put(
        self,
        source_type: str,
        ref: str,
        path: str,
        body: bytes,
        ttl_seconds: int,
    ) -> bool:
        """Store body. Returns True on write, False on rejection/error.

        Rejects empty bodies, oversized bodies, non-positive TTLs, and
        (when ``fetch_cache_max_keys > 0``) writes that would push the
        key count above the configured cap. Cardinality is sampled at
        most once per ``fetch_cache_count_interval_s``.
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
        # §17.133 — cardinality cap. 0 disables. SCAN failure returns -1,
        # which we ignore (fail open: a Redis hiccup must not block puts).
        max_keys = settings.fetch_cache_max_keys
        if max_keys > 0:
            count = await self._key_count()
            if count >= max_keys:
                self._capped += 1
                logger.warning(
                    "fetch_cache_cardinality_capped: key=%s count=%d cap=%d",
                    key, count, max_keys,
                )
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
            "capped": self._capped,
            "last_count": self._last_count,
        }


_cache: FetchCache | None = None


def get_fetch_cache() -> FetchCache:
    """Module-level singleton accessor."""
    global _cache
    if _cache is None:
        _cache = FetchCache()
    return _cache


async def close_fetch_cache() -> None:
    """§17.855 (audit B7) — close the singleton's aioredis client at shutdown.

    The four Redis-backed caches each hold a lazily-opened aioredis connection
    that was never closed (harmless on process exit, but a leak in tests /
    reloads and an unclean-shutdown warning). The lifespan now closes all four.
    """
    global _cache
    if _cache is not None and _cache._redis is not None:
        try:
            await _cache._redis.aclose()
        except Exception:  # noqa: BLE001 — shutdown best-effort
            pass
        _cache._redis = None
