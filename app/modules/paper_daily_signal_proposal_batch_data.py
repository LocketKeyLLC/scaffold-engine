# app/modules/paper_daily_signal_proposal_batch_data.py
"""Batch data access for the daily signal-proposal pipeline.

Reads the point-in-time signal stream and produces the equity/order-count
context the daily batch runner needs, without duplicating the audit-trail
schema or the signal/proposal logic owned by sibling modules.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_day_start_equity_recording import get_day_start_equity
from app.modules.paper_point_time_store import get_data_as_of
from app.modules.paper_signal_generator import generate_signals

logger = logging.getLogger("scaffold")


async def load_batch_context(
    *,
    timestamp: datetime,
    trading_day: date,
    db: Optional[Any] = None,
) -> dict[str, Any]:
    """Load the equity and order-count context for a daily batch run.

    Returns a dict with ``day_start_equity`` (Decimal or None) and
    ``orders_today`` (int).  The caller combines this with signals from
    ``generate_signals`` to drive ``propose_trades``.
    """
    own_db = db is None
    if own_db:
        db = async_session()

    try:
        day_start_equity = await get_day_start_equity(db=db, trading_day=trading_day)

        # Count orders already placed today from the broker_order_id audit
        # trail.  The daily order limit is enforced downstream by
        # paper_risk_control_module; here we only report the count.
        result = await db.execute(
            text(
                "SELECT COUNT(*) FROM broker_order_id "
                "WHERE submitted_ts >= :day_start "
                "AND submitted_ts < :day_end"
            ),
            {
                "day_start": datetime.combine(
                    trading_day, datetime.min.time(), tzinfo=timezone.utc
                ),
                "day_end": datetime.combine(
                    trading_day, datetime.min.time(), tzinfo=timezone.utc
                )
                + __import__("datetime").timedelta(days=1),
            },
        )
        orders_today = int(result.scalar_one())

        return {
            "day_start_equity": day_start_equity,
            "orders_today": orders_today,
        }
    finally:
        if own_db:
            await db.close()


async def load_batch_signals(
    *,
    timestamp: datetime,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Load signals for a daily batch run.

    Thin wrapper over ``paper_signal_generator.generate_signals`` so the
    batch runner has a single data-access entry point.
    """
    return await generate_signals(timestamp, db=db)


__all__ = ["load_batch_context", "load_batch_signals"]
