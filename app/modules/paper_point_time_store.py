# app/modules/paper_point_time_store.py
"""Point-in-time retrieval from the paper audit trail.

The audit trail stores every ingested item with its publication timestamp.
This module exposes a single read path, get_data_as_of, that returns only
items whose published_ts is on or before the requested timestamp, so a
strategy can never see data published after its cutoff.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold")


async def get_data_as_of(
    timestamp: datetime,
    *,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Return payloads from ingested_item with published_ts <= timestamp.

    The comparison is inclusive: a row whose published_ts equals timestamp
    is included. Rows are returned in insertion order (by id) for
    deterministic callers.
    """
    query = text(
        "SELECT payload "
        "FROM ingested_item "
        "WHERE published_ts <= :ts "
        "ORDER BY id"
    )
    params = {"ts": timestamp}

    if db is not None:
        result = await db.execute(query, params)
        rows = result.fetchall()
    else:
        async with async_session() as session:
            result = await session.execute(query, params)
            rows = result.fetchall()

    return [row[0] for row in rows]
