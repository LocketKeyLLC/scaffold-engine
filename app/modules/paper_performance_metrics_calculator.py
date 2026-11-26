# app/modules/paper_performance_metrics_calculator.py
"""Paper account performance metrics calculator.

Computes return after costs, max drawdown, Sharpe ratio, and turnover for
the paper trading account, and compares against the SPY benchmark.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import pairwise
from typing import Any, Optional, Sequence

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_pnl
from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark

logger = logging.getLogger("scaffold")

ZERO = Decimal("0")
TRADING_DAYS_PER_YEAR = 252
#: §17.2045 — a Sharpe ratio needs a spread of daily returns: fewer than this and the ratio is not reported (None),
#: never 0 (which reads as a measured, flat strategy)
MIN_DAILY_RETURNS_FOR_SHARPE = 2


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _opt_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _closing_equity_by_day(rows: Sequence[Any]) -> list[tuple[date, Decimal]]:
    """§17.2045 — each UTC day's closing equity: its last pnl row's equity (rows come oldest first).

    The lab writes a day-start row (13:00 UTC), the after-close P&L row and then the metrics row (21:30 UTC), so a
    day's last row is its close. A row without a positive equity is not a reading.
    """
    closes: dict[date, Decimal] = {}
    for row in rows:
        ts = row["as_of_ts"]
        equity = _to_decimal(row["equity"])
        if ts is None or equity <= ZERO:
            continue
        closes[ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date()] = equity
    return sorted(closes.items())


def daily_returns(rows: Sequence[Any]) -> list[Decimal]:
    """§17.2045 — the account's daily returns from its own equity series: each close over the close before it."""
    closes = [equity for _day, equity in _closing_equity_by_day(rows)]
    return [(b - a) / a for a, b in pairwise(closes)]


def sharpe_ratio(returns: Sequence[Decimal]) -> tuple[Optional[Decimal], Optional[str]]:
    """§17.2045 — the annualised Sharpe ratio (risk-free rate 0, sqrt(252)) and, when there is none, why."""
    n = len(returns)
    if n < MIN_DAILY_RETURNS_FOR_SHARPE:
        return None, (f"not enough data: {n} daily return{'s' if n != 1 else ''} recorded, "
                      f"{MIN_DAILY_RETURNS_FOR_SHARPE} needed")
    mean_return = sum(returns) / Decimal(n)
    variance = sum((r - mean_return) ** 2 for r in returns) / Decimal(n - 1)
    if variance <= ZERO:
        return None, f"undefined: the {n} daily returns do not vary"
    return (mean_return / variance.sqrt()) * Decimal(TRADING_DAYS_PER_YEAR).sqrt(), None


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
            "sharpe_ratio": None,
            "sharpe_ratio_note": sharpe_ratio([])[1],
            "turnover": ZERO,
            "spy_benchmark_return": ZERO,
            "account_value": ZERO,
            "cash": ZERO,
            "equity": ZERO,
            "realized_pnl": ZERO,
            "unrealized_pnl": ZERO,
            "daily_return": None,
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

    # §17.2045 — Sharpe ratio from the account's own equity series (a day's close over the close before it). It was
    # read from `daily_return`, which no writer fills, so it stayed 0; None (with the reason) when it cannot be said.
    returns = daily_returns(rows)
    sharpe, sharpe_note = sharpe_ratio(returns)

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

    metrics: dict[str, Any] = {
        "return_after_costs": return_after_costs,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe,
        "turnover": turnover,
        "spy_benchmark_return": spy_benchmark_return,
        "account_value": final_account_value,
        "cash": _to_decimal(last["cash"]),
        "equity": _to_decimal(last["equity"]),
        "realized_pnl": _to_decimal(last["realized_pnl"]),
        "unrealized_pnl": _to_decimal(last["unrealized_pnl"]),
        "daily_return": returns[-1] if returns else None,
        "cumulative_return": cumulative_return,
    }
    if sharpe_note:
        metrics["sharpe_ratio_note"] = sharpe_note
    return metrics


async def get_performance_metrics() -> dict[str, Any]:
    """Compute and persist performance metrics for the paper account."""
    async with async_session() as db:
        metrics = await _get_performance_metrics_with_db(db)

        # Strings for write_pnl; a metric that cannot be said (§17.2045: no Sharpe yet) is stored NULL, not "None"
        await write_pnl(
            as_of_ts=datetime.now(timezone.utc),
            account_value=str(metrics["account_value"]),
            cash=str(metrics["cash"]),
            equity=str(metrics["equity"]),
            realized_pnl=str(metrics["realized_pnl"]),
            unrealized_pnl=str(metrics["unrealized_pnl"]),
            daily_return=_opt_str(metrics["daily_return"]),
            cumulative_return=str(metrics["cumulative_return"]),
            max_drawdown=str(metrics["max_drawdown"]),
            sharpe_ratio=_opt_str(metrics["sharpe_ratio"]),
            turnover=str(metrics["turnover"]),
            spy_benchmark_return=str(metrics["spy_benchmark_return"]),
        )

        return metrics
