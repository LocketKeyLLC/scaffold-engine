# app/modules/paper_spy_benchmark_tracker.py
"""SPY benchmark tracker for the paper trading audit trail.

Fetches SPY historical bars via the Alpaca paper client's market-data
endpoint, computes buy-and-hold return after costs, max drawdown, Sharpe
ratio, and turnover, and returns benchmark metrics for comparison against
paper strategy PnL.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

logger = logging.getLogger("scaffold")

SPY_PRICE_URL = "https://data.alpaca.markets/v2/stocks/bars"
SPY_SECRET_NAME = "SPY_PRICE_API_KEY"
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.0
COST_PER_TRADE = 0.0001  # 1 bp per trade, applied to buy and sell


async def _fetch_spy_prices(
    start_date: datetime,
    end_date: datetime,
    *,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Fetch SPY daily bars from the Alpaca paper client.

    Returns a list of dicts with keys 'timestamp' (datetime) and 'close'
    (float), sorted ascending by timestamp.
    """
    client = AlpacaPaperClient()
    try:
        bars = await client.get_bars(
            "SPY",
            timeframe="1Day",
            start=start_date,
            end=end_date,
        )
    finally:
        await client.close()

    prices: list[dict[str, Any]] = []
    for bar in bars:
        ts = datetime.fromisoformat(bar["t"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        prices.append(
            {
                "timestamp": ts,
                "close": float(bar["c"]),
            }
        )

    prices.sort(key=lambda p: p["timestamp"])
    return prices


def _buy_and_hold_return(prices: list[dict[str, Any]]) -> float:
    """Compute buy-and-hold return after costs.

    Cost is applied as a round-trip cost on the initial buy and final sell.
    """
    if not prices:
        return 0.0
    first_close = float(prices[0]["close"])
    last_close = float(prices[-1]["close"])
    if first_close <= 0:
        raise ValueError("SPY price must be positive")
    gross_return = (last_close / first_close) - 1.0
    net_return = gross_return - (2 * COST_PER_TRADE)
    return net_return


def _max_drawdown(prices: list[dict[str, Any]]) -> float:
    """Compute maximum drawdown as a negative fraction (e.g., -0.15 = 15% drawdown)."""
    if not prices:
        return 0.0
    peak = float(prices[0]["close"])
    max_dd = 0.0
    for p in prices:
        close = float(p["close"])
        if close > peak:
            peak = close
        dd = (close - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    return max_dd


def _sharpe_ratio(prices: list[dict[str, Any]]) -> float:
    """Compute annualized Sharpe ratio from daily returns."""
    if len(prices) < 2:
        return 0.0

    daily_returns: list[float] = []
    for i in range(1, len(prices)):
        prev = float(prices[i - 1]["close"])
        curr = float(prices[i]["close"])
        if prev <= 0:
            continue
        daily_returns.append((curr / prev) - 1.0)

    if not daily_returns:
        return 0.0

    mean_return = sum(daily_returns) / len(daily_returns)
    variance = sum((r - mean_return) ** 2 for r in daily_returns) / len(daily_returns)
    std_dev = variance**0.5

    if std_dev == 0:
        return 0.0

    annualized_return = mean_return * TRADING_DAYS_PER_YEAR
    annualized_std = std_dev * (TRADING_DAYS_PER_YEAR**0.5)
    return (annualized_return - RISK_FREE_RATE) / annualized_std


def _turnover(prices: list[dict[str, Any]]) -> float:
    """Compute turnover. Buy-and-hold has zero turnover."""
    return 0.0


async def get_spy_benchmark(
    start_date: datetime,
    end_date: datetime,
    *,
    db: Optional[Any] = None,
) -> dict[str, Any]:
    """Return SPY benchmark metrics for the given date range.

    Returns a dict with keys:
        start_date, end_date, buy_and_hold_return, max_drawdown,
        sharpe_ratio, turnover, num_prices
    """
    prices = await _fetch_spy_prices(start_date, end_date, db=db)

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "buy_and_hold_return": _buy_and_hold_return(prices),
        "max_drawdown": _max_drawdown(prices),
        "sharpe_ratio": _sharpe_ratio(prices),
        "turnover": _turnover(prices),
        "num_prices": len(prices),
    }
