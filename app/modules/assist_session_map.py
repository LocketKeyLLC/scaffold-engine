"""Per-chat assist-session memory.

Maps an OWUI chat_id to its currently active assist session (and last
seen node_key). Lets the OWUI pipeline drop the requirement that the
user paste `<session_id>` into every subcommand.

Redis-only: this is ephemeral chat-UX state, not durable session state.
The authoritative session lives in `assist_sessions`. If Redis is down,
the pipeline falls back to explicit-arg behaviour (which still works).

TTL aligns with `settings.assist_idle_threshold_days` so a stale chat
mapping cannot outlive the assist session it points at.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger("scaffold.assist_session_map")

_KEY_PREFIX = "assist:chatmap:v1"

_redis: aioredis.Redis | None = None


def _key(chat_id: str) -> str:
    return f"{_KEY_PREFIX}:{chat_id}"


async def _client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def _ttl_seconds() -> int:
    return settings.assist_idle_threshold_days * 24 * 60 * 60


async def remember(
    chat_id: str,
    *,
    session_id: str,
    last_node_key: Optional[str] = None,
) -> None:
    """Store (or refresh) the chat→session mapping.

    Passing `last_node_key=None` preserves the previously stored value
    rather than clearing it — the typical caller updates one field at a
    time (start sets session_id; next sets last_node_key).
    """
    try:
        r = await _client()
        existing_raw = await r.get(_key(chat_id))
        existing = json.loads(existing_raw) if existing_raw else {}
        merged = {
            "session_id": session_id,
            "last_node_key": (
                last_node_key
                if last_node_key is not None
                else existing.get("last_node_key")
            ),
        }
        await r.set(_key(chat_id), json.dumps(merged), ex=_ttl_seconds())
    except Exception as exc:
        logger.warning("assist_session_map.remember failed: %s", exc)


async def recall(chat_id: str) -> Optional[dict]:
    """Return `{"session_id": ..., "last_node_key": ...}` or None."""
    try:
        r = await _client()
        raw = await r.get(_key(chat_id))
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:
        logger.warning("assist_session_map.recall failed: %s", exc)
        return None


async def forget(chat_id: str) -> None:
    try:
        r = await _client()
        await r.delete(_key(chat_id))
    except Exception as exc:
        logger.warning("assist_session_map.forget failed: %s", exc)
