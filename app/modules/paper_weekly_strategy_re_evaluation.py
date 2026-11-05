# app/modules/paper_weekly_strategy_re_evaluation.py
"""Weekly strategy re-evaluation for the paper trading engine.

Runs a walk-forward backtest over the last 1-2 years of data and decides
whether the strategy should continue trading or be paused pending operator
approval.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from app.modules.paper_walk_forward_backtester import INITIAL_EQUITY, run_backtest

logger = logging.getLogger("scaffold")

DEFAULT_LOOKBACK_YEARS = 2
PAUSE_THRESHOLD = Decimal("0")

__all__ = ["re_evaluate_strategy"]


def _extract_return_fraction(backtest_result: dict[str, Any]) -> Decimal | None:
    """Return the strategy's total return fraction from a backtest result.

    Prefers an explicit ``total_return`` key, then falls back to the equity
    curve or final equity value.
    """
    total_return = backtest_result.get("total_return")
    if total_return is not None:
        return Decimal(str(total_return))

    equity_curve = backtest_result.get("equity_curve")
    if equity_curve and len(equity_curve) >= 2:
        initial = Decimal(str(equity_curve[0]))
        final = Decimal(str(equity_curve[-1]))
        if initial == 0:
            return None
        return (final - initial) / initial

    if equity_curve and len(equity_curve) == 1:
        return Decimal("0")

    final_equity = backtest_result.get("final_equity")
    if final_equity is not None:
        final = Decimal(str(final_equity))
        if INITIAL_EQUITY == 0:
            return None
        return (final - INITIAL_EQUITY) / INITIAL_EQUITY

    return None


async def re_evaluate_strategy(
    *,
    db: Optional[Any] = None,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
) -> dict[str, Any]:
    """Run a weekly backtest and decide whether to continue or pause.

    Args:
        db: Optional database session to pass to the backtester.
        lookback_years: How many years of historical data to backtest.
            Must be at least 1.

    Returns:
        A dictionary with the decision (``"continue"`` or ``"pause"``),
        the backtest metrics used, and a human-readable reason.
    """
    if lookback_years < 1:
        raise ValueError("lookback_years must be at least 1")

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=365 * lookback_years)

    logger.info(
        "Weekly strategy re-evaluation: backtesting from %s to %s",
        start_date.isoformat(),
        end_date.isoformat(),
    )

    try:
        backtest_result = await run_backtest(start_date, end_date, db=db)
    except Exception as exc:
        logger.exception("Backtest failed during weekly strategy re-evaluation")
        return {
            "decision": "pause",
            "paused": True,
            "start_date": start_date,
            "end_date": end_date,
            "lookback_years": lookback_years,
            "total_return": None,
            "reason": (
                f"Backtest error: {exc}. "
                "Strategy paused; operator approval required to resume."
            ),
        }

    return_fraction = _extract_return_fraction(backtest_result)

    if return_fraction is None:
        logger.error(
            "Backtest result missing return metrics: %s",
            backtest_result.keys(),
        )
        return {
            "decision": "pause",
            "paused": True,
            "start_date": start_date,
            "end_date": end_date,
            "lookback_years": lookback_years,
            "total_return": None,
            "reason": (
                "Backtest result missing return metrics; "
                "operator approval required to resume."
            ),
        }

    if return_fraction > PAUSE_THRESHOLD:
        decision = "continue"
        paused = False
        reason = (
            f"Backtest returned {return_fraction:.4%} over the last "
            f"{lookback_years} year(s); strategy may continue."
        )
    else:
        decision = "pause"
        paused = True
        reason = (
            f"Backtest shows the strategy would lose money "
            f"({return_fraction:.4%}) over the last {lookback_years} year(s). "
            "Strategy paused; operator approval required to resume."
        )

    logger.info("Weekly strategy re-evaluation result: %s (%s)", decision, reason)

    return {
        "decision": decision,
        "paused": paused,
        "start_date": start_date,
        "end_date": end_date,
        "lookback_years": lookback_years,
        "total_return": float(return_fraction),
        "reason": reason,
    }
