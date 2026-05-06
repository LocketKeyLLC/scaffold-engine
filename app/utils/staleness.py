"""Staleness sweeper for TOON entries.

Queries entries with ``expires_at > 0 AND expires_at < now()``, then deletes
them in paginated batches. Pagination is cursor-based on ``entry_id`` so a
stuck or delayed flush cannot re-surface the same IDs and loop.

Sentinel: ``expires_at == 0`` is treated as "never expire" and is excluded
from sweeps by the ``expires_at > 0`` predicate. Callers that want to mark
an entry as never-expiring should persist ``0``.
"""
from __future__ import annotations

import asyncio
import logging
import time

from app.utils.milvus_utils import get_collection
from app.config import settings, TTL_POLICY, DEFAULT_TTL_SECONDS

logger = logging.getLogger("scaffold.staleness")

COLLECTION_NAME = "toon_v2"


async def sweep_expired() -> dict:
    """Delete entries where ``expires_at > 0 AND expires_at < now``.

    Cursor-based pagination on ``entry_id`` guarantees forward progress even
    if a batch's ``flush()`` lags behind the subsequent query.
    """
    loop = asyncio.get_running_loop()
    col = await loop.run_in_executor(None, get_collection)
    if col is None:
        return {"status": "error", "error": "collection not available"}
    now = int(time.time())

    def _sync() -> dict:
        _PAGE_SIZE = 1000
        _MAX_PAGES = 100  # safety cap: 100k entries per sweep
        _TITLES_CAP = 50
        total_ids: list[str] = []
        total_titles: list[str] = []
        hit_cap = True
        last_id = ""  # all stringly-typed entry_ids sort after the empty string

        for _ in range(_MAX_PAGES):
            # #49 cursor pagination: entry_id > last_id prevents re-seeing rows
            # whose delete hasn't been flushed yet.
            expr = (
                f'expires_at > 0 and expires_at < {now} '
                f'and entry_id > "{last_id}"'
            )
            expired = col.query(
                expr=expr,
                output_fields=["entry_id", "title", "source_type", "expires_at"],
                limit=_PAGE_SIZE,
            )
            if not expired:
                hit_cap = False
                break

            ids = [e["entry_id"] for e in expired]
            # #48 IN expression with explicit double-quoted, escaped IDs
            quoted = ",".join(
                '"' + eid.replace('\\', '\\\\').replace('"', '\\"') + '"'
                for eid in ids
            )
            col.delete(expr=f"entry_id in [{quoted}]")
            col.flush()

            total_ids.extend(ids)
            total_titles.extend(e.get("title", "unknown") for e in expired)
            # Advance cursor to the max entry_id in this batch.
            last_id = max(ids)

            if len(expired) < _PAGE_SIZE:
                hit_cap = False
                break

        if hit_cap:
            logger.error(
                "staleness_sweep: hit MAX_PAGES=%d cap, more expired entries may remain",
                _MAX_PAGES,
            )
        logger.info("staleness_sweep: deleted %d expired entries", len(total_ids))
        return {
            "status": "ok",
            "expired_count": len(total_ids),
            "deleted": total_titles[:_TITLES_CAP],
            "deleted_truncated": len(total_titles) > _TITLES_CAP,
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
        Absolute expiry epoch (seconds). Callers that want an entry to never
        expire should persist ``0`` directly rather than calling this function.
    """
    ttl = get_ttl_for_source(source_type)
    base = created_at if created_at is not None else int(time.time())
    return base + ttl
