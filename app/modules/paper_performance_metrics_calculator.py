# app/modules/paper_performance_metrics_calculator.py
"""Paper account performance metrics calculator.

Computes return after costs, max drawdown, Sharpe ratio, and turnover for
the paper trading account, and compares against the SPY benchmark.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_pnl
from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark

logger = logging.getLogger("scaffold")

ZERO = Decimal("0")
TRADING_DAYS_PER_YEAR = 252


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


async def _get_performance_metrics_with_db(db: Any) -> dict[str, Any]:
    """Fetch pnl snapshots and compute performance metrics."""
    rows = (await db.execute(text(
        "SELECT as_of_ts, account_value, cash, equity, realized_pnl, "
        "unrealized_pnl, daily_return, cumulative_return, max_drawdown, "
        "sharpe_ratio, turnover, spy_benchmark_return "
        "FROM pnl ORDER BY as_of_ts ASC"
    ))).mappings().all()

    if not rows:
        return {
            "return_after_costs": ZERO,
            "max_drawdown": ZERO,
            "sharpe_ratio": ZERO,
            "turnover": ZERO,
            "spy_benchmark_return": ZERO,
            "account_value": ZERO,
            "cash": ZERO,
            "equity": ZERO,
            "realized_pnl": ZERO,
            "unrealized_pnl": ZERO,
            "daily_return": ZERO,
            "cumulative_return": ZERO,
        }

    first = rows[0]
    last = rows[-1]

    initial_account_value = _to_decimal(first["account_value"])
    final_account_value = _to_decimal(last["account_value"])

    # Cumulative return (fractional)
    if initial_account_value > ZERO:
        cumulative_return = (final_account_value - initial_account_value) / initial_account_value
    else:
        cumulative_return = ZERO

    # Costs: total commissions from fills
    fill_rows = (await db.execute(text(
        "SELECT commission, quantity, price FROM fill"
    ))).mappings().all()
    total_commission = sum(_to_decimal(row["commission"]) for row in fill_rows)

    cost_fraction = ZERO
    if initial_account_value > ZERO:
        cost_fraction = total_commission / initial_account_value

    return_after_costs = cumulative_return - cost_fraction

    # Max drawdown: from pnl snapshots
    max_drawdown = ZERO
    peak = ZERO
    for row in rows:
        equity = _to_decimal(row["equity"])
        if equity > peak:
            peak = equity
        if peak > ZERO:
            drawdown = (equity - peak) / peak
            if drawdown < max_drawdown:
                max_drawdown = drawdown

    # Sharpe ratio: from daily_return snapshots
    daily_returns = [_to_decimal(row["daily_return"]) for row in rows if row["daily_return"] is not None]
    if len(daily_returns) >= 2:
        mean_return = sum(daily_returns) / Decimal(len(daily_returns))
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / Decimal(len(daily_returns) - 1)
        std_dev = variance.sqrt() if variance > ZERO else ZERO
        if std_dev > ZERO:
            sharpe_ratio = (mean_return / std_dev) * Decimal(TRADING_DAYS_PER_YEAR).sqrt()
        else:
            sharpe_ratio = ZERO
    else:
        sharpe_ratio = ZERO

    # Turnover: traded value divided by average account value
    total_traded_value = ZERO
    for row in fill_rows:
        qty = _to_decimal(row["quantity"])
        price = _to_decimal(row["price"])
        total_traded_value += qty * price

    account_values = [_to_decimal(row["account_value"]) for row in rows]
    if account_values:
        avg_account_value = sum(account_values) / Decimal(len(account_values))
    else:
        avg_account_value = ZERO

    if avg_account_value > ZERO:
        turnover = total_traded_value / avg_account_value
    else:
        turnover = ZERO

    # SPY benchmark return
    start_ts = first["as_of_ts"]
    end_ts = last["as_of_ts"]
    if start_ts is not None and end_ts is not None and start_ts < end_ts:
        spy_metrics = await get_spy_benchmark(start_ts, end_ts, db=db)
        spy_benchmark_return = _to_decimal(spy_metrics.get("buy_and_hold_return", ZERO))
    else:
        spy_benchmark_return = ZERO

    return {
        "return_after_costs": return_after_costs,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "turnover": turnover,
        "spy_benchmark_return": spy_benchmark_return,
        "account_value": final_account_value,
        "cash": _to_decimal(last["cash"]),
        "equity": _to_decimal(last["equity"]),
        "realized_pnl": _to_decimal(last["realized_pnl"]),
        "unrealized_pnl": _to_decimal(last["unrealized_pnl"]),
        "daily_return": _to_decimal(last["daily_return"]),
        "cumulative_return": cumulative_return,
    }


async def get_performance_metrics() -> dict[str, Any]:
    """Compute and persist performance metrics for the paper account."""
    async with async_session() as db:
        metrics = await _get_performance_metrics_with_db(db)

        # Convert all metrics to strings for write_pnl (expects str)
        await write_pnl(
            as_of_ts=datetime.now(timezone.utc),
            account_value=str(metrics["account_value"]),
            cash=str(metrics["cash"]),
            equity=str(metrics["equity"]),
            realized_pnl=str(metrics["realized_pnl"]),
            unrealized_pnl=str(metrics["unrealized_pnl"]),
            daily_return=str(metrics["daily_return"]),
            cumulative_return=str(metrics["cumulative_return"]),
            max_drawdown=str(metrics["max_drawdown"]),
            sharpe_ratio=str(metrics["sharpe_ratio"]),
            turnover=str(metrics["turnover"]),
            spy_benchmark_return=str(metrics["spy_benchmark_return"]),
        )

        return metrics
