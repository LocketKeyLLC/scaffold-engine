"""Staleness sweeper for TOON entries.

Queries entries with expires_at > 0 AND expires_at < now(),
then deletes or logs them based on policy.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pymilvus import Collection, connections, utility

from app.config import settings

logger = logging.getLogger("scaffold.staleness")

COLLECTION_NAME = "toon_v2"

# TTL defaults by source_type (seconds)
TTL_POLICY = {
    "real_time": 7 * 86400,        # 7 days
    "news": 30 * 86400,            # 30 days
    "community": 90 * 86400,       # 90 days
    "tech_docs": 180 * 86400,      # 6 months
    "curated": 365 * 86400,        # 1 year
    "official_docs": 365 * 86400,  # 1 year
    "ai_generated": 180 * 86400,   # 6 months
}


def _get_collection() -> Collection | None:
    try:
        try:
            utility.list_collections()
        except Exception:
            connections.connect(alias="default", uri=settings.milvus_uri)
        if not utility.has_collection(COLLECTION_NAME):
            return None
        col = Collection(COLLECTION_NAME)
        col.load()
        return col
    except Exception as e:
        logger.error("staleness: failed to get collection: %s", e)
        return None


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
        return {
            "status": "ok",
            "expired_count": len(ids),
            "deleted": titles,
        }

    return await loop.run_in_executor(None, _sync)


def get_ttl_for_source(source_type: str) -> int:
    """Return TTL in seconds for a given source type."""
    return TTL_POLICY.get(source_type, 180 * 86400)


def compute_expires_at(source_type: str, created_at: int = 0) -> int:
    """Compute expires_at timestamp. Returns 0 for no-expiry."""
    ttl = get_ttl_for_source(source_type)
    base = created_at or int(time.time())
    return base + ttl
