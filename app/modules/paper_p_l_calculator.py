# app/modules/paper_p_l_calculator.py
"""Deterministic realized and unrealized P&L per position from fills.

Computes P&L from the audit-trail `fill` and `pnl` tables. Realized P&L is
computed on position closes (FIFO); unrealized P&L marks open positions to
the most recent fill price. Results are written to the `pnl` table and
returned to the caller.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_pnl

logger = logging.getLogger("scaffold")

# A position's realized P&L is computed only when it is closed (FIFO).
# Open positions are marked to the most recent fill price for unrealized P&L.
ZERO = Decimal("0")
# Precision for unrealized P&L quantization (two decimal places).
PRECISION = Decimal("0.01")


@dataclass(frozen=True)
class PnlRecord:
    """A single position's P&L record, as returned by `calculate_pnl`."""

    ticker: str
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_quantity: Decimal
    average_entry_price: Decimal | None
    last_price: Decimal | None


@dataclass
class _Position:
    """Internal mutable position tracker."""

    ticker: str
    quantity: Decimal = field(default_factory=lambda: ZERO)
    realized_pnl: Decimal = field(default_factory=lambda: ZERO)
    lots: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (qty, price)

    def add_lot(self, qty: Decimal, price: Decimal) -> None:
        self.quantity += qty
        self.lots.append((qty, price))

    def close_lot(self, qty: Decimal, price: Decimal) -> None:
        """FIFO close of `qty` shares at `price`, updating realized P&L."""
        remaining = qty
        while remaining > ZERO and self.lots:
            lot_qty, lot_price = self.lots[0]
            if lot_qty <= remaining:
                self.realized_pnl += lot_qty * (price - lot_price)
                remaining -= lot_qty
                self.lots.pop(0)
            else:
                self.realized_pnl += remaining * (price - lot_price)
                self.lots[0] = (lot_qty - remaining, lot_price)
                remaining = ZERO
        self.quantity -= qty


async def _fetch_fills(db: object) -> list[dict[str, Any]]:
    """Fetch all fills ordered by filled_ts, oldest first."""
    result = await db.execute(
        text(
            """
            SELECT p.ticker, f.side, f.quantity, f.price, f.filled_ts
            FROM fill f
            JOIN broker_order_id b ON f.broker_order_id = b.id
            JOIN proposal p ON b.proposal_id = p.id
            ORDER BY f.filled_ts ASC, f.id ASC
            """
        )
    )
    rows = result.fetchall()
    return [
        {
            "ticker": row.ticker,
            "side": row.side,
            "quantity": Decimal(str(row.quantity)),
            "price": Decimal(str(row.price)),
            "filled_ts": row.filled_ts,
        }
        for row in rows
    ]


def _compute_positions(fills: list[dict[str, Any]]) -> dict[str, _Position]:
    """Replay fills FIFO to produce per-ticker position state."""
    positions: dict[str, _Position] = {}
    for fill in fills:
        ticker = fill["ticker"]
        side = fill["side"]
        qty = fill["quantity"]
        price = fill["price"]
        pos = positions.setdefault(ticker, _Position(ticker=ticker))
        if side == "buy":
            pos.add_lot(qty, price)
        elif side == "sell":
            pos.close_lot(qty, price)
        else:
            logger.warning("Ignoring fill with unexpected side %r", side)
    return positions


def _average_entry_price(pos: _Position) -> Decimal | None:
    if not pos.lots:
        return None
    total_qty = sum((qty for qty, _ in pos.lots), ZERO)
    if total_qty == ZERO:
        return None
    total_cost = sum((qty * price for qty, price in pos.lots), ZERO)
    return total_cost / total_qty


async def _latest_price(db: object, ticker: str) -> Decimal | None:
    """Return the most recent fill price for a ticker, or None."""
    result = await db.execute(
        text(
            """
            SELECT f.price
            FROM fill f
            JOIN broker_order_id b ON f.broker_order_id = b.id
            JOIN proposal p ON b.proposal_id = p.id
            WHERE p.ticker = :ticker
            ORDER BY f.filled_ts DESC, f.id DESC
            LIMIT 1
            """
        ),
        {"ticker": ticker},
    )
    row = result.fetchone()
    return Decimal(str(row.price)) if row is not None else None


async def _latest_pnl_ts(db: object) -> datetime | None:
    result = await db.execute(text("SELECT MAX(as_of_ts) FROM pnl"))
    row = result.fetchone()
    return row[0] if row is not None else None


async def calculate_pnl() -> list[PnlRecord]:
    """Compute realized and unrealized P&L per position from fills.

    Writes a `pnl` row for the current account state and returns the
    per-position records.
    """
    async with async_session() as db:
        fills = await _fetch_fills(db)
        positions = _compute_positions(fills)

        records: list[PnlRecord] = []
        total_realized = ZERO
        total_unrealized = ZERO
        total_equity = ZERO

        for ticker, pos in positions.items():
            avg_entry = _average_entry_price(pos)
            last_price = await _latest_price(db, ticker)
            open_qty = pos.quantity
            if avg_entry is not None and last_price is not None and open_qty > ZERO:
                unrealized = (open_qty * (last_price - avg_entry)).quantize(PRECISION)
            else:
                unrealized = ZERO
            total_realized += pos.realized_pnl
            total_unrealized += unrealized
            if last_price is not None and open_qty > ZERO:
                total_equity += open_qty * last_price
            records.append(
                PnlRecord(
                    ticker=ticker,
                    realized_pnl=pos.realized_pnl,
                    unrealized_pnl=unrealized,
                    open_quantity=open_qty,
                    average_entry_price=avg_entry,
                    last_price=last_price,
                )
            )

        # Cash is not tracked in this module; assume zero cash for the
        # pnl row. The performance-metrics module owns full account value.
        now = datetime.now(timezone.utc)
        await write_pnl(
            as_of_ts=now,
            account_value=str(total_equity),
            cash="0",
            equity=str(total_equity),
            realized_pnl=str(total_realized),
            unrealized_pnl=str(total_unrealized),
            daily_return=None,
            cumulative_return=None,
            max_drawdown=None,
            sharpe_ratio=None,
            turnover=None,
            spy_benchmark_return=None,
        )
        await db.commit()
        return records
