"""RAG retrieval-result cache.

Skips the embed → vector + keyword → RRF → rerank pipeline for repeated
identical queries within a TTL window. The reranker is the dominant CPU
cost on CPU-only hosts (CrossEncoder ~50 s for 12 candidates on the
reference T480), so even a single hit during a multi-node execution
pays for the cache wholesale.

Key format: ``ragv1:{domain_or_all}:{sha256(canonical_payload)}``

- ``ragv1`` prefix — bump on response-shape or key-composition change.
- Canonical payload is ``json.dumps({query, domain, top_k,
  confidence_threshold, skip_rerank, include_history, query_intent},
  sort_keys=True)``. Any drift in retrieval params invalidates the entry.
- Domain segment lifts the partition into the key path so the operator
  can ``SCAN MATCH ragv1:eng:*`` to drop one domain's cache on ingest.

Value: full ``query_rag`` response dict (status / query / result_count /
results / metadata), JSON-serialized.

Skip rules — entries that should NOT be cached:

- ``status != "ok"`` — error path. Caching errors masks real failures.
- ``metadata.warnings`` non-empty — anomalous path (embed_failed,
  reranker timeout, supersede sweep glitch). Re-run on next call.
- ``metadata.below_threshold == True`` — confidence filter relaxed.
  These results are deliberately marginal; serving a stale fallback
  from cache could mask retrieval-quality drift the operator is
  watching for.
- value size exceeds ``settings.rag_result_cache_max_value_bytes`` —
  guards Redis against a 50-chunk pathological response.

Fail-open: every Redis error is logged + swallowed. Cache misses + Redis
unavailability both produce a None return; callers run the pipeline.

Gated by ``settings.cache_rag_results`` (default False).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("scaffold.rag_result_cache")

_KEY_PREFIX = "ragv1"


def _canonical_payload(
    query: str,
    domain: str | None,
    top_k: int,
    confidence_threshold: float,
    skip_rerank: bool,
    include_history: bool,
    query_intent: str,
    max_candidates: int | None = None,
    doc_truncate: int | None = None,
    domain_hint: str | None = None,
) -> str:
    # §17.604 — domain_hint narrows the search fan-out (_iter_search_domains),
    # so two calls with the same (query, domain) but different hints retrieve
    # from different partition sets → different results. Omitting it from the
    # key let `domain=None, hint='eng'` collide with `hint=None`. Same
    # default=None / explicit-value-keys-differently semantic as below.
    # §17.234 — max_candidates added with default=None for backward-
    # compat with existing cached entries (a call that doesn't pass it
    # gets the same key as pre-§17.234). Explicit values produce a
    # different key so a max_candidates=5 request doesn't hit cache from
    # a max_candidates=32 request and vice versa.
    # §17.252 — doc_truncate added with the same default=None /
    # explicit-value-keys-differently semantic. A doc_truncate=250
    # request reranks against shorter doc representations than a
    # doc_truncate=2000 request → different shortlist → different cache
    # row.
    return json.dumps(
        {
            "query": query,
            "domain": domain,
            "top_k": top_k,
            "confidence_threshold": confidence_threshold,
            "skip_rerank": skip_rerank,
            "include_history": include_history,
            "query_intent": query_intent,
            "max_candidates": max_candidates,
            "doc_truncate": doc_truncate,
            "domain_hint": domain_hint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def make_key(
    query: str,
    domain: str | None,
    top_k: int,
    confidence_threshold: float,
    skip_rerank: bool,
    include_history: bool,
    query_intent: str,
    max_candidates: int | None = None,
    doc_truncate: int | None = None,
    domain_hint: str | None = None,
) -> str:
    """Build the Redis key for a query_rag call."""
    payload = _canonical_payload(
        query, domain, top_k, confidence_threshold,
        skip_rerank, include_history, query_intent,
        max_candidates=max_candidates,
        doc_truncate=doc_truncate,
        domain_hint=domain_hint,
    )
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    domain_seg = domain if domain else "all"
    return f"{_KEY_PREFIX}:{domain_seg}:{h}"


def _is_cacheable(response: dict[str, Any]) -> bool:
    """Decide whether a response is safe to cache."""
    if response.get("status") != "ok":
        return False
    meta = response.get("metadata") or {}
    if meta.get("warnings"):
        return False
    if meta.get("below_threshold"):
        return False
    return True


class RagResultCache:
    """Redis-backed cache for query_rag responses. Fail-open."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.redis_url
        self._redis: aioredis.Redis | None = None
        self._hits = 0
        self._misses = 0
        self._puts = 0
        self._skipped = 0
        self._oversized = 0
        self._uncacheable = 0

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self.redis_url, decode_responses=False)
        return self._redis

    async def get(
        self,
        query: str,
        domain: str | None,
        top_k: int,
        confidence_threshold: float,
        skip_rerank: bool,
        include_history: bool,
        query_intent: str,
        *,
        max_candidates: int | None = None,
        doc_truncate: int | None = None,
        domain_hint: str | None = None,
    ) -> dict[str, Any] | None:
        """Return cached response or None on miss / gate-off / Redis error."""
        if not settings.cache_rag_results:
            self._skipped += 1
            return None

        key = make_key(
            query, domain, top_k, confidence_threshold,
            skip_rerank, include_history, query_intent,
            max_candidates=max_candidates,
            doc_truncate=doc_truncate,
            domain_hint=domain_hint,
        )
        try:
            r = await self._get_redis()
            blob = await r.get(key)
        except Exception as e:
            logger.warning("rag_result_cache_get_failed: key=%s err=%s", key, e)
            return None

        if not blob:
            self._misses += 1
            return None

        try:
            response = json.loads(blob)
            if not isinstance(response, dict):
                raise ValueError("payload not a dict")
        except (ValueError, TypeError) as e:
            logger.warning("rag_result_cache_bad_payload: key=%s err=%s", key, e)
            try:
                await r.delete(key)
            except Exception:
                pass
            self._misses += 1
            return None

        self._hits += 1
        return response

    async def put(
        self,
        query: str,
        domain: str | None,
        top_k: int,
        confidence_threshold: float,
        skip_rerank: bool,
        include_history: bool,
        query_intent: str,
        response: dict[str, Any],
        *,
        max_candidates: int | None = None,
        doc_truncate: int | None = None,
        domain_hint: str | None = None,
    ) -> bool:
        """Store a response. Returns True on write, False on skip/error.

        Only clean ``status=ok`` responses without warnings or
        below_threshold are cached — see module docstring.
        """
        if not settings.cache_rag_results:
            return False
        if not _is_cacheable(response):
            self._uncacheable += 1
            return False

        key = make_key(
            query, domain, top_k, confidence_threshold,
            skip_rerank, include_history, query_intent,
            max_candidates=max_candidates,
            doc_truncate=doc_truncate,
            domain_hint=domain_hint,
        )
        try:
            value = json.dumps(response, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as e:
            logger.warning("rag_result_cache_encode_failed: key=%s err=%s", key, e)
            return False

        max_bytes = settings.rag_result_cache_max_value_bytes
        if len(value) > max_bytes:
            self._oversized += 1
            logger.warning(
                "rag_result_cache_oversized: key=%s bytes=%d cap=%d",
                key, len(value), max_bytes,
            )
            return False

        try:
            r = await self._get_redis()
            await r.set(key, value, ex=settings.rag_result_cache_ttl_s)
        except Exception as e:
            logger.warning("rag_result_cache_put_failed: key=%s err=%s", key, e)
            return False
        self._puts += 1
        return True

    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "puts": self._puts,
            "skipped": self._skipped,
            "uncacheable": self._uncacheable,
            "oversized": self._oversized,
        }


_cache: RagResultCache | None = None


def get_rag_result_cache() -> RagResultCache:
    """Module-level singleton accessor."""
    global _cache
    if _cache is None:
        _cache = RagResultCache()
    return _cache
