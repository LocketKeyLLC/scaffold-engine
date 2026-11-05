# app/modules/paper_walk_forward_backtester.py
"""Walk-forward backtester for the paper trading engine.

Splits the date range into non-overlapping train/test windows, retrains a
model on each train window, applies it to test items, and computes
performance metrics including a SPY benchmark comparison.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from app.modules.paper_point_time_store import get_data_as_of
from app.modules.paper_signal_generator import generate_signals
from app.modules.paper_trade_proposer import (
    MAX_PROPOSALS_PER_BATCH,
    propose_trades,
)
from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark

logger = logging.getLogger("scaffold")

# Walk-forward configuration
TRAIN_WINDOW_DAYS = 60
TEST_WINDOW_DAYS = 20

# Fee and slippage (as fractions of notional)
FEE_RATE = Decimal("0.0001")
SLIPPAGE_RATE = Decimal("0.0002")

# Starting equity for the backtest
INITIAL_EQUITY = Decimal("100000.0")


def _default_model_trainer(train_items: list[dict[str, Any]]) -> Callable[[dict[str, Any]], bool]:
    """Train a simple model that accepts items whose payload has a 'score' >= 0.5.

    Returns a callable that takes an item and returns True if the model
    accepts it (i.e., it should generate a signal).
    """
    def _apply(item: dict[str, Any]) -> bool:
        payload = item.get("payload", {})
        if not isinstance(payload, dict):
            return False
        score = payload.get("score")
        if score is None:
            return False
        try:
            return float(score) >= 0.5
        except (TypeError, ValueError):
            return False

    return _apply


def _filter_by_publication_time(
    items: list[dict[str, Any]],
    as_of_date: datetime,
) -> list[dict[str, Any]]:
    """Filter items strictly on publication_time <= as_of_date.

    Items missing publication_time are skipped.
    """
    filtered: list[dict[str, Any]] = []
    for item in items:
        pub_time = item.get("publication_time")
        if pub_time is None:
            continue
        try:
            if isinstance(pub_time, str):
                pub_time = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
            if pub_time.tzinfo is None:
                pub_time = pub_time.replace(tzinfo=timezone.utc)
            if pub_time <= as_of_date:
                filtered.append(item)
        except (TypeError, ValueError, AttributeError):
            continue
    return filtered


def _mark_to_market(
    holdings: dict[str, Decimal],
    prices: dict[str, Decimal],
    cash: Decimal,
) -> Decimal:
    """Compute equity by marking holdings to market using last available price."""
    equity = cash
    for symbol, quantity in holdings.items():
        price = prices.get(symbol)
        if price is not None:
            equity += quantity * price
    return equity


def _compute_turnover(
    trade_notionals: list[Decimal],
    average_equity: Decimal,
) -> float:
    """Compute turnover as sum(abs(trade_notional)) / average_equity."""
    if average_equity <= 0:
        return 0.0
    total_notional = sum(abs(n) for n in trade_notionals)
    return float(total_notional / average_equity)


def _compute_total_return(equity_curve: list[Decimal]) -> float:
    """Compute total return from the equity curve."""
    if not equity_curve or equity_curve[0] == 0:
        return 0.0
    return float((equity_curve[-1] - equity_curve[0]) / equity_curve[0])


def _compute_max_drawdown(equity_curve: list[Decimal]) -> float:
    """Compute maximum drawdown as a negative fraction."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = Decimal("0.0")
    for equity in equity_curve:
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (equity - peak) / peak
            if dd < max_dd:
                max_dd = dd
    return float(max_dd)


def _compute_sharpe_ratio(equity_curve: list[Decimal]) -> float:
    """Compute annualized Sharpe ratio from daily returns."""
    if len(equity_curve) < 2:
        return 0.0
    returns: list[float] = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] == 0:
            continue
        returns.append(float((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]))
    if not returns:
        return 0.0
    mean_return = sum(returns) / len(returns)
    if len(returns) < 2:
        return 0.0
    variance = sum((r - mean_return) ** 2 for r in returns) / (len(returns) - 1)
    std_dev = variance ** 0.5
    if std_dev == 0:
        return 0.0
    return (mean_return / std_dev) * (252 ** 0.5)


def _compute_benchmark_curve(
    spy_metrics: dict[str, Any],
    dates: list[datetime],
) -> list[Decimal]:
    """Build a benchmark equity curve from SPY metrics."""
    spy_total_return = spy_metrics.get("buy_and_hold_return", 0.0)
    if not dates:
        return []
    n = len(dates)
    if n < 2:
        return [Decimal("1.0")] * n
    curve: list[Decimal] = []
    start_value = Decimal("1.0")
    end_value = start_value * (Decimal("1.0") + Decimal(str(spy_total_return)))
    for i in range(n):
        fraction = Decimal(i) / Decimal(n - 1)
        curve.append(start_value + (end_value - start_value) * fraction)
    return curve


async def run_backtest(
    start_date: datetime,
    end_date: datetime,
    *,
    db: Optional[Any] = None,
) -> dict[str, Any]:
    """Run a walk-forward backtest over the given date range.

    Returns a dict with performance metrics for the strategy and the SPY
    benchmark.
    """
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    if end_date <= start_date:
        raise ValueError("end_date must be after start_date")

    dates: list[datetime] = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)

    train_windows: list[tuple[datetime, datetime]] = []
    test_windows: list[tuple[datetime, datetime]] = []
    window_start = start_date
    while window_start < end_date:
        train_end = window_start + timedelta(days=TRAIN_WINDOW_DAYS)
        if train_end > end_date:
            train_end = end_date
        test_end = train_end + timedelta(days=TEST_WINDOW_DAYS)
        if test_end > end_date:
            test_end = end_date
        if train_end > window_start:
            train_windows.append((window_start, train_end))
        if test_end > train_end:
            test_windows.append((train_end, test_end))
        window_start = test_end

    cash = INITIAL_EQUITY
    holdings: dict[str, Decimal] = {}
    equity_curve: list[Decimal] = []
    trade_notionals: list[Decimal] = []
    trade_count = 0
    orders_today = 0
    last_date: Optional[datetime] = None

    for train_window, test_window in zip(train_windows, test_windows, strict=False):
        train_items = await get_data_as_of(train_window[1], db=db)
        train_items = _filter_by_publication_time(train_items, train_window[1])
        model = _default_model_trainer(train_items)

        test_items = await get_data_as_of(test_window[1], db=db)
        test_items = _filter_by_publication_time(test_items, test_window[1])

        signals = await generate_signals(test_window[1], db=db)

        filtered_signals: list[dict[str, Any]] = []
        for signal in signals:
            if model(signal):
                filtered_signals.append(signal)

        day_start_equity = _mark_to_market(holdings, {}, cash)
        proposals = await propose_trades(
            filtered_signals,
            equity=day_start_equity,
            day_start_equity=day_start_equity,
            orders_today=orders_today,
            db=db,
        )

        for proposal in proposals:
            if orders_today >= MAX_PROPOSALS_PER_BATCH:
                break
            ticker = proposal.get("ticker", "")
            side = proposal.get("side", "buy")
            quantity = Decimal(str(proposal.get("quantity", 0)))
            limit_price = Decimal(str(proposal.get("limit_price", 0)))

            if quantity <= 0 or limit_price <= 0:
                continue

            notional = quantity * limit_price
            fee = notional * FEE_RATE
            slippage = notional * SLIPPAGE_RATE

            if side == "buy":
                total_cost = notional + fee + slippage
                if total_cost <= cash:
                    cash -= total_cost
                    holdings[ticker] = holdings.get(ticker, Decimal("0")) + quantity
                    trade_notionals.append(notional)
                    trade_count += 1
                    orders_today += 1
            elif side == "sell":
                if ticker in holdings and holdings[ticker] >= quantity:
                    cash += notional - fee - slippage
                    holdings[ticker] -= quantity
                    if holdings[ticker] == 0:
                        del holdings[ticker]
                    trade_notionals.append(notional)
                    trade_count += 1
                    orders_today += 1

        prices: dict[str, Decimal] = {}
        for proposal in proposals:
            ticker = proposal.get("ticker", "")
            limit_price = Decimal(str(proposal.get("limit_price", 0)))
            if ticker and limit_price > 0:
                prices[ticker] = limit_price

        equity = _mark_to_market(holdings, prices, cash)
        equity_curve.append(equity)

        orders_today = 0
        last_date = test_window[1]

    if not equity_curve:
        all_items = await get_data_as_of(end_date, db=db)
        all_items = _filter_by_publication_time(all_items, end_date)
        model = _default_model_trainer(all_items)
        signals = await generate_signals(end_date, db=db)
        filtered_signals = [s for s in signals if model(s)]
        day_start_equity = _mark_to_market(holdings, {}, cash)
        proposals = await propose_trades(
            filtered_signals,
            equity=day_start_equity,
            day_start_equity=day_start_equity,
            orders_today=orders_today,
            db=db,
        )
        for proposal in proposals:
            if orders_today >= MAX_PROPOSALS_PER_BATCH:
                break
            ticker = proposal.get("ticker", "")
            side = proposal.get("side", "buy")
            quantity = Decimal(str(proposal.get("quantity", 0)))
            limit_price = Decimal(str(proposal.get("limit_price", 0)))
            if quantity <= 0 or limit_price <= 0:
                continue
            notional = quantity * limit_price
            fee = notional * FEE_RATE
            slippage = notional * SLIPPAGE_RATE
            if side == "buy":
                total_cost = notional + fee + slippage
                if total_cost <= cash:
                    cash -= total_cost
                    holdings[ticker] = holdings.get(ticker, Decimal("0")) + quantity
                    trade_notionals.append(notional)
                    trade_count += 1
                    orders_today += 1
            elif side == "sell":
                if ticker in holdings and holdings[ticker] >= quantity:
                    cash += notional - fee - slippage
                    holdings[ticker] -= quantity
                    if holdings[ticker] == 0:
                        del holdings[ticker]
                    trade_notionals.append(notional)
                    trade_count += 1
                    orders_today += 1
        prices = {}
        for proposal in proposals:
            ticker = proposal.get("ticker", "")
            limit_price = Decimal(str(proposal.get("limit_price", 0)))
            if ticker and limit_price > 0:
                prices[ticker] = limit_price
        equity = _mark_to_market(holdings, prices, cash)
        equity_curve.append(equity)

    if equity_curve:
        average_equity = sum(equity_curve) / len(equity_curve)
    else:
        average_equity = INITIAL_EQUITY

    total_return = _compute_total_return(equity_curve)
    max_drawdown = _compute_max_drawdown(equity_curve)
    sharpe_ratio = _compute_sharpe_ratio(equity_curve)
    turnover = _compute_turnover(trade_notionals, average_equity)

    spy_metrics = await get_spy_benchmark(start_date, end_date, db=db)
    spy_total_return = float(spy_metrics.get("buy_and_hold_return", 0.0))
    spy_max_drawdown = float(spy_metrics.get("max_drawdown", 0.0))
    spy_sharpe_ratio = float(spy_metrics.get("sharpe_ratio", 0.0))
    spy_turnover = float(spy_metrics.get("turnover", 0.0))

    benchmark_curve = _compute_benchmark_curve(spy_metrics, dates)

    return {
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "turnover": turnover,
        "spy_total_return": spy_total_return,
        "spy_max_drawdown": spy_max_drawdown,
        "spy_sharpe_ratio": spy_sharpe_ratio,
        "spy_turnover": spy_turnover,
        "trade_count": trade_count,
        "equity_curve": [float(e) for e in equity_curve],
        "benchmark_curve": [float(b) for b in benchmark_curve],
    }


__all__ = [
    "MAX_PROPOSALS_PER_BATCH",
    "FEE_RATE",
    "SLIPPAGE_RATE",
    "INITIAL_EQUITY",
    "TRAIN_WINDOW_DAYS",
    "TEST_WINDOW_DAYS",
    "run_backtest",
    "_default_model_trainer",
    "_filter_by_publication_time",
    "_mark_to_market",
    "_compute_turnover",
    "_compute_total_return",
    "_compute_max_drawdown",
    "_compute_sharpe_ratio",
    "_compute_benchmark_curve",
]
