"""Verifier-verdict cache.

Caches the deterministic ``_verify_output`` 3-tuple ``(status, reason,
confidence)`` keyed by the canonicalized verifier inputs. Cuts the
redundant verifier LLM call when an identical ``(task_title, output,
tool_schema, model, temperature)`` tuple is seen again — most commonly
during a retry storm where the same upstream output gets re-verified
against the same prompt.

Key format: ``llmverifyv1:{model}:{sha256(canonical_payload)}``

- ``llmverifyv1`` prefix — verifier-only, distinct from ``embedv3`` /
  ``fetchv1``. Bump on contract change (cache shape, key composition).
- Canonical payload is ``json.dumps(..., sort_keys=True)`` over
  ``{messages, tool_schema, temperature, model}`` so any input drift
  invalidates the entry.

Value: ``json.dumps({"status": "pass"|"fail", "reason": str,
"confidence": float})``. Only ``pass`` verdicts are cached — a ``fail``
verdict on retry has different W.1 feedback context and must re-run.

Fail-open: every Redis error is logged + swallowed. Cache misses + Redis
unavailability both produce a None return; callers proceed to call the
LLM as if the cache didn't exist.

Gated by ``settings.cache_llm_responses`` (default False). When the gate
is off, ``get`` returns None and ``put`` is a no-op — no Redis traffic.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("scaffold.llm_response_cache")

_KEY_PREFIX = "llmverifyv1"

VerdictStatus = Literal["pass", "fail"]


def _canonical_payload(
    messages: list[dict[str, str]],
    tool_schema: dict[str, Any],
    model: str,
    temperature: float,
) -> str:
    """Stable JSON encoding of the cache-key inputs."""
    return json.dumps(
        {
            "messages": messages,
            "tool_schema": tool_schema,
            "model": model,
            "temperature": temperature,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def make_key(
    messages: list[dict[str, str]],
    tool_schema: dict[str, Any],
    model: str,
    temperature: float,
) -> str:
    """Build the Redis key for a verifier call."""
    payload = _canonical_payload(messages, tool_schema, model, temperature)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{model}:{h}"


class VerifierCache:
    """Redis-backed cache for verifier verdicts. Fail-open."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or settings.redis_url
        self._redis: aioredis.Redis | None = None
        self._hits = 0
        self._misses = 0
        self._puts = 0
        self._skipped = 0  # gate off — counted separately from misses

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self.redis_url, decode_responses=False)
        return self._redis

    async def get(
        self,
        messages: list[dict[str, str]],
        tool_schema: dict[str, Any],
        model: str,
        temperature: float,
    ) -> tuple[VerdictStatus, str, float] | None:
        """Return cached verdict tuple or None on miss / gate-off / Redis error."""
        if not settings.cache_llm_responses:
            self._skipped += 1
            return None

        key = make_key(messages, tool_schema, model, temperature)
        try:
            r = await self._get_redis()
            blob = await r.get(key)
        except Exception as e:
            logger.warning("llm_response_cache_get_failed: key=%s err=%s", key, e)
            return None

        if not blob:
            self._misses += 1
            return None

        try:
            parsed = json.loads(blob)
            status = parsed["status"]
            reason = parsed["reason"]
            confidence = float(parsed["confidence"])
            if status not in ("pass", "fail"):
                raise ValueError(f"bad status {status!r}")
        except (ValueError, KeyError, TypeError) as e:
            # Stale or corrupt payload — drop the key and miss.
            logger.warning("llm_response_cache_bad_payload: key=%s err=%s", key, e)
            try:
                await r.delete(key)
            except Exception:
                pass
            self._misses += 1
            return None

        self._hits += 1
        return status, reason, confidence

    async def put(
        self,
        messages: list[dict[str, str]],
        tool_schema: dict[str, Any],
        model: str,
        temperature: float,
        status: VerdictStatus,
        reason: str,
        confidence: float,
    ) -> bool:
        """Store a verdict. Returns True on write, False on skip/error.

        Only ``pass`` verdicts are stored. ``fail`` verdicts must re-run
        on retry because the W.1 feedback-injection path means the next
        attempt's prompt context differs from this one's.
        """
        if not settings.cache_llm_responses:
            return False
        if status != "pass":
            return False

        key = make_key(messages, tool_schema, model, temperature)
        value = json.dumps(
            {"status": status, "reason": reason, "confidence": confidence},
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            r = await self._get_redis()
            await r.set(key, value, ex=settings.llm_response_cache_ttl_s)
        except Exception as e:
            logger.warning("llm_response_cache_put_failed: key=%s err=%s", key, e)
            return False
        self._puts += 1
        return True

    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "puts": self._puts,
            "skipped": self._skipped,
        }


_cache: VerifierCache | None = None


def get_verifier_cache() -> VerifierCache:
    """Module-level singleton accessor."""
    global _cache
    if _cache is None:
        _cache = VerifierCache()
    return _cache
