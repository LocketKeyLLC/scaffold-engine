# app/modules/paper_day_start_equity_recording.py
"""Record account equity at the start of each trading day into the audit trail.

The daily-loss limit in paper_risk_control_module.check_daily_loss reads the
day-start equity recorded here. The daily batch entry point records the value
at the start of its run and reads it back when proposing trades.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold")


async def record_day_start_equity(
    *,
    db,
    equity: Decimal,
    cash: Optional[Decimal] = None,
    account_value: Optional[Decimal] = None,
    as_of_ts: Optional[datetime] = None,
) -> int:
    """Record the account equity at the start of a trading day.

    Inserts one pnl row with as_of_ts at day start and equity = day-start
    equity. cash and account_value default to equity when not supplied.

    Idempotent per UTC date: if a pnl row already exists for the same UTC
    date, returns that row's id without inserting a duplicate.

    Returns the pnl row id.
    """
    equity = Decimal(str(equity))
    if equity <= 0:
        raise ValueError("equity must be positive")

    if as_of_ts is None:
        as_of_ts = datetime.now(timezone.utc)
    if as_of_ts.tzinfo is None:
        as_of_ts = as_of_ts.replace(tzinfo=timezone.utc)

    if cash is None:
        cash = equity
    else:
        cash = Decimal(str(cash))
    if account_value is None:
        account_value = equity
    else:
        account_value = Decimal(str(account_value))

    trading_day = as_of_ts.date()

    existing = await db.execute(
        text(
            """
            SELECT id FROM pnl
            WHERE as_of_ts >= :day_start AND as_of_ts < :day_end
            ORDER BY as_of_ts ASC
            LIMIT 1
            """
        ),
        {
            "day_start": datetime.combine(trading_day, time.min, tzinfo=timezone.utc),
            "day_end": datetime.combine(
                trading_day + timedelta(days=1), time.min, tzinfo=timezone.utc
            ),
        },
    )
    existing_id = existing.scalar()
    if existing_id is not None:
        logger.info(
            "Day-start equity already recorded for %s (pnl id %s); skipping",
            trading_day,
            existing_id,
        )
        return int(existing_id)

    result = await db.execute(
        text(
            """
            INSERT INTO pnl (as_of_ts, account_value, cash, equity)
            VALUES (:as_of_ts, :account_value, :cash, :equity)
            RETURNING id
            """
        ),
        {
            "as_of_ts": as_of_ts,
            "account_value": account_value,
            "cash": cash,
            "equity": equity,
        },
    )
    row_id = int(result.scalar_one())
    await db.commit()
    logger.info(
        "Recorded day-start equity %s for %s (pnl id %s)",
        equity,
        trading_day,
        row_id,
    )
    return row_id


async def get_day_start_equity(
    *,
    db,
    trading_day: date,
) -> Optional[Decimal]:
    """Return the day-start equity recorded for a trading day, or None.

    Reads the earliest pnl row on the UTC date of trading_day.
    """
    day_start = datetime.combine(trading_day, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(
        trading_day + timedelta(days=1), time.min, tzinfo=timezone.utc
    )

    result = await db.execute(
        text(
            """
            SELECT equity FROM pnl
            WHERE as_of_ts >= :day_start AND as_of_ts < :day_end
            ORDER BY as_of_ts ASC
            LIMIT 1
            """
        ),
        {"day_start": day_start, "day_end": day_end},
    )
    row = result.scalar()
    if row is None:
        return None
    return Decimal(str(row))


__all__ = [
    "get_day_start_equity",
    "record_day_start_equity",
]
