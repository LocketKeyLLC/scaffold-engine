"""Staleness sweeper for TOON entries.

Queries entries with expires_at > 0 AND expires_at < now(),
then deletes or logs them based on policy.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pymilvus import Collection
from app.utils.milvus_utils import get_collection

from app.config import settings, TTL_POLICY, DEFAULT_TTL_SECONDS

logger = logging.getLogger("scaffold.staleness")

COLLECTION_NAME = "toon_v2"

_get_collection = get_collection


async def sweep_expired() -> dict:
    """Delete entries where expires_at > 0 AND expires_at < now."""
    loop = asyncio.get_running_loop()
    col = await loop.run_in_executor(None, _get_collection)
    if col is None:
        return {"status": "error", "error": "collection not available"}

    now = int(time.time())

    def _sync() -> dict:
        expired = col.query(
            expr=f"expires_at > 0 and expires_at < {now}",
            output_fields=["entry_id", "title", "source_type", "expires_at"],
            limit=1000,
        )
        if not expired:
            return {"status": "ok", "expired_count": 0, "deleted": []}

        ids = [e["entry_id"] for e in expired]
        col.delete(expr=f'entry_id in {ids}')
        col.flush()

        titles = [e.get("title", "unknown") for e in expired]
        logger.info("staleness_sweep: deleted %d expired entries", len(ids))
        _TITLES_CAP = 50
        return {
            "status": "ok",
            "expired_count": len(ids),
            "deleted": titles[:_TITLES_CAP],
            "deleted_truncated": len(titles) > _TITLES_CAP,
        }

    return await loop.run_in_executor(None, _sync)


def get_ttl_for_source(source_type: str) -> int:
    """Return TTL in seconds for a given source type.

    Logs a warning for unknown source_types and falls back to
    ``DEFAULT_TTL_SECONDS`` (180 days).
    """
    if source_type not in TTL_POLICY:
        logger.warning(
            "staleness_unknown_source_type: source_type=%r falling_back_to=%ds",
            source_type, DEFAULT_TTL_SECONDS,
        )
        return DEFAULT_TTL_SECONDS
    return TTL_POLICY[source_type]


def compute_expires_at(source_type: str, created_at: int | None = None) -> int:
    """Compute expires_at timestamp.

    Args:
        source_type: TOON source category; drives TTL selection.
        created_at: Entry creation epoch (seconds). ``None`` (default) uses now.

    Returns:
        Absolute expiry epoch (seconds).
    """
    ttl = get_ttl_for_source(source_type)
    base = created_at if created_at is not None else int(time.time())
    return base + ttl
