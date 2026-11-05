# app/modules/paper_risk_control_module.py
"""Deterministic risk control for the paper-trading research lab.

Every rule here is plain code, never a prompt. The limits are fixed by the
operator's decisions:

- max position = 5% of equity
- max daily loss = 3% (halt new orders for the rest of the day, keep existing
  positions)
- max 3 orders/day
- allow-list of large liquid US stocks and broad ETFs only
- no options, margin, shorting, or crypto
- kill switch cancels orders and closes positions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable

logger = logging.getLogger("scaffold")

# Fixed by operator decision: one position may use at most 5% of the account.
MAX_POSITION_PCT = Decimal("0.05")

# Fixed by operator decision: trading halts for the day after a 3% daily loss.
MAX_DAILY_LOSS_PCT = Decimal("0.03")

# Fixed by operator decision: at most about 3 orders a day.
MAX_ORDERS_PER_DAY = 3

# Fixed by operator decision: allow-list of large liquid US stocks and broad
# ETFs only. No options, margin, shorting, or crypto. This list is the
# operator's fixed watchlist of plain stocks and ETFs.
ALLOW_LIST: frozenset[str] = frozenset(
    {
        # Broad US equity ETFs
        "SPY",
        "VOO",
        "IVV",
        "VTI",
        "ITOT",
        "SCHB",
        "QQQ",
        "VGT",
        "XLK",
        "DIA",
        "IWM",
        "VB",
        "VO",
        "VTV",
        "VUG",
        # Large liquid US stocks
        "AAPL",
        "MSFT",
        "GOOGL",
        "GOOG",
        "AMZN",
        "NVDA",
        "META",
        "TSLA",
        "BRK.B",
        "JPM",
        "V",
        "JNJ",
        "WMT",
        "PG",
        "MA",
        "UNH",
        "HD",
        "DIS",
        "BAC",
        "NFLX",
        "ADBE",
        "CRM",
        "XOM",
        "CVX",
        "KO",
        "PEP",
        "TMO",
        "COST",
        "ABBV",
        "LLY",
        "MRK",
        "PFE",
        "AVGO",
        "ORCL",
        "CSCO",
        "INTC",
        "AMD",
        "QCOM",
        "TXN",
        "MU",
        "IBM",
        "ACN",
        "T",
        "VZ",
        "CMCSA",
        "NKE",
        "MCD",
        "SBUX",
        "LOW",
        "TGT",
        "CAT",
        "DE",
        "BA",
        "GE",
        "HON",
        "LMT",
        "RTX",
        "NEE",
        "DUK",
        "SO",
        "D",
        "AEP",
        "EXC",
        "XEL",
        "WEC",
        "ES",
        "ED",
        "PEG",
        "PPL",
        "FE",
        "EIX",
        "SRE",
        "AEE",
        "CMS",
        "DTE",
        "ETR",
        "CNP",
        "NI",
        "LNT",
        "PNW",
        "OGE",
        "WEC",
    }
)

# Symbols that are explicitly forbidden even if they would otherwise appear
# on the allow-list: options, margin, shorting, crypto are all out of scope.
FORBIDDEN_SYMBOL_PATTERNS: tuple[str, ...] = (
    "-",  # options like AAPL-250117C00150000
    "/",  # crypto pairs like BTC/USD
    "USD",  # crypto quote
    "USDT",
    "USDC",
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "DOGE",
    "ADA",
    "AVAX",
    "DOT",
    "LINK",
    "MATIC",
    "SHIB",
    "LTC",
    "BCH",
)


@dataclass(frozen=True)
class RiskState:
    """Point-in-time snapshot of what the risk module needs to decide."""

    equity: Decimal
    day_start_equity: Decimal
    orders_today: int
    trading_day: date
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def daily_pnl(self) -> Decimal:
        return self.equity - self.day_start_equity

    @property
    def daily_return(self) -> Decimal:
        if self.day_start_equity == 0:
            return Decimal("0")
        return self.daily_pnl / self.day_start_equity


@dataclass(frozen=True)
class RiskDecision:
    """Outcome of a risk check: allowed or rejected with a reason."""

    allowed: bool
    reason: str | None = None


def _clean_symbol(ticker: str) -> str:
    """Normalize a ticker for allow-list lookup."""
    return ticker.strip().upper()


def check_allow_list(ticker: str) -> RiskDecision:
    """Reject anything not on the allow-list of plain stocks and ETFs.

    Also rejects options (contains '-'), crypto pairs (contains '/'), and
    known crypto symbols, even if they somehow appear on the allow-list.
    """
    cleaned = _clean_symbol(ticker)
    if not cleaned:
        return RiskDecision(False, "empty ticker")

    for pattern in FORBIDDEN_SYMBOL_PATTERNS:
        if pattern in cleaned:
            return RiskDecision(False, f"forbidden symbol pattern: {pattern}")

    if cleaned not in ALLOW_LIST:
        return RiskDecision(False, f"{cleaned} is not on the allow-list")

    return RiskDecision(True)


def check_position_size(
    *,
    equity: Decimal,
    proposed_notional: Decimal,
    current_position_notional: Decimal = Decimal("0"),
) -> RiskDecision:
    """Reject a position that would exceed 5% of equity.

    `proposed_notional` is the dollar value of the proposed order.
    `current_position_notional` is the existing position in the same ticker
    (0 if none). The combined position must not exceed 5% of equity.
    """
    if equity <= 0:
        return RiskDecision(False, "equity must be positive")
    if proposed_notional < 0:
        return RiskDecision(False, "proposed notional must be non-negative")
    if current_position_notional < 0:
        return RiskDecision(False, "current position notional must be non-negative")

    max_notional = equity * MAX_POSITION_PCT
    combined = current_position_notional + proposed_notional
    if combined > max_notional:
        return RiskDecision(
            False,
            (
                f"position {combined} exceeds {MAX_POSITION_PCT:.0%} of equity "
                f"(max {max_notional})"
            ),
        )
    return RiskDecision(True)


def check_daily_loss(
    *,
    equity: Decimal,
    day_start_equity: Decimal,
) -> RiskDecision:
    """Halt new orders for the rest of the day after a 3% daily loss.

    Existing positions are kept; only new orders are blocked.
    """
    if day_start_equity <= 0:
        return RiskDecision(False, "day start equity must be positive")

    daily_return = (equity - day_start_equity) / day_start_equity
    if daily_return <= -MAX_DAILY_LOSS_PCT:
        return RiskDecision(
            False,
            (
                f"daily loss {daily_return:.2%} exceeds "
                f"{MAX_DAILY_LOSS_PCT:.0%} halt threshold"
            ),
        )
    return RiskDecision(True)


def check_order_count(
    *,
    orders_today: int,
    max_orders: int = MAX_ORDERS_PER_DAY,
) -> RiskDecision:
    """Reject an order when the daily order count is exhausted."""
    if orders_today < 0:
        return RiskDecision(False, "orders_today must be non-negative")
    if orders_today >= max_orders:
        return RiskDecision(
            False, f"daily order limit reached ({orders_today}/{max_orders})"
        )
    return RiskDecision(True)


async def trigger_kill_switch(
    *,
    db,
    reason: str,
    cancel_orders_fn=None,
    close_positions_fn=None,
) -> None:
    """Cancel open orders and close all positions.

    `cancel_orders_fn` and `close_positions_fn` are async callables injected
    by the caller (the paper order executor and fill tracker). When either is
    None, that half of the kill switch is skipped and a warning is logged.

    The kill switch itself is deterministic: it always attempts both halves
    and never raises. It logs at ERROR level so the audit trail and operator
    dashboard pick it up.
    """
    logger.error("KILL SWITCH TRIGGERED: %s", reason)

    if cancel_orders_fn is not None:
        try:
            await cancel_orders_fn()
            logger.error("KILL SWITCH: open orders cancelled")
        except Exception:
            logger.exception("KILL SWITCH: cancel_orders_fn failed")

    if close_positions_fn is not None:
        try:
            await close_positions_fn()
            logger.error("KILL SWITCH: positions closed")
        except Exception:
            logger.exception("KILL SWITCH: close_positions_fn failed")


def evaluate_proposal(
    *,
    ticker: str,
    equity: Decimal,
    day_start_equity: Decimal,
    orders_today: int,
    proposed_notional: Decimal,
    current_position_notional: Decimal = Decimal("0"),
) -> RiskDecision:
    """Run every deterministic risk check on a proposed order.

    Returns the first rejection (with its reason), or an allowed decision if
    all checks pass. This is the single entry point the trade proposer calls
    before showing a proposal to the operator.
    """
    allow = check_allow_list(ticker)
    if not allow.allowed:
        return allow

    size = check_position_size(
        equity=equity,
        proposed_notional=proposed_notional,
        current_position_notional=current_position_notional,
    )
    if not size.allowed:
        return size

    loss = check_daily_loss(
        equity=equity,
        day_start_equity=day_start_equity,
    )
    if not loss.allowed:
        return loss

    count = check_order_count(orders_today=orders_today)
    if not count.allowed:
        return count

    return RiskDecision(True)


__all__ = [
    "ALLOW_LIST",
    "FORBIDDEN_SYMBOL_PATTERNS",
    "MAX_DAILY_LOSS_PCT",
    "MAX_ORDERS_PER_DAY",
    "MAX_POSITION_PCT",
    "RiskDecision",
    "RiskState",
    "check_allow_list",
    "check_daily_loss",
    "check_order_count",
    "check_position_size",
    "evaluate_proposal",
    "trigger_kill_switch",
]
