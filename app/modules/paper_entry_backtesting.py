# app/modules/paper_entry_backtesting.py
"""Entry-point backtesting for the paper trading engine.

Runs a walk-forward backtest over the most recent data window and
returns the result for the engine's entry pipeline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_walk_forward_backtester import run_backtest

logger = logging.getLogger("scaffold")

DEFAULT_LOOKBACK_DAYS = 60


async def _latest_data_timestamp(db: object) -> Optional[datetime]:
    """Return the max publication timestamp across ingested items."""
    result = await db.execute(
        text("SELECT MAX(published_ts) AS max_ts FROM ingested_item")
    )
    row = result.mappings().first()
    if row is None or row["max_ts"] is None:
        return None
    return row["max_ts"]


async def run_backtesting_entry_point(
    *,
    db: Optional[object] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Run a walk-forward backtest over the latest data window.

    Determines `start_date` and `end_date` from the most recent
    `published_ts` in `ingested_item`.  When the table is empty,
    falls back to a fixed window ending at the current UTC time.
    """
    own_session = db is None
    if db is None:
        db = async_session()

    try:
        latest_ts = await _latest_data_timestamp(db)
        now = datetime.now(timezone.utc)

        if latest_ts is not None:
            end_date = latest_ts
            start_date = latest_ts - timedelta(days=lookback_days)
        else:
            end_date = now
            start_date = now - timedelta(days=lookback_days)

        logger.info(
            "Running entry backtest from %s to %s", start_date, end_date
        )

        # Always pass start_date and end_date as keyword arguments.
        return await run_backtest(
            start_date=start_date,
            end_date=end_date,
            db=db,
        )
    finally:
        if own_session:
            await db.close()


__all__ = ["run_backtesting_entry_point", "_latest_data_timestamp"]
