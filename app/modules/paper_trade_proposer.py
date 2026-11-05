# app/modules/paper_trade_proposer.py
"""Paper trade proposer: turns signals into concrete trade proposals.

The proposer aggregates signals by ticker (preferring long over flat),
calls a model to propose a limit price and quantity, and falls back to a
deterministic proposal if the model fails or returns invalid data.

This module only proposes; it does not write to the database or contact
the broker. The caller is responsible for writing proposals via the audit
trail writer.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from app.model_router import generate_json

logger = logging.getLogger("scaffold")

# Fixed values that must never change at runtime.
PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"

# Maximum number of proposals a batch may generate (imported by backtester).
MAX_PROPOSALS_PER_BATCH: int = 3

# Deterministic fallback notional (operator decision: about $100).
FALLBACK_NOTIONAL: Decimal = Decimal("100")
FALLBACK_LIMIT_PRICE: Decimal = Decimal("1.0")

# JSON schema for the model's proposal response.
_RATIONALE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit_price": {"type": "number", "minimum": 0},
        "quantity": {"type": "number", "minimum": 0},
        "rationale": {"type": "string"},
    },
    "required": ["limit_price", "quantity", "rationale"],
}


async def propose_trades(
    signals: list,
    *,
    equity: Decimal,
    day_start_equity: Decimal,
    orders_today: int,
    db: Any,
) -> List[Dict[str, Any]]:
    """Propose trades for a batch of signals.

    Returns a list of proposal dicts, one per ticker (after aggregation).
    Each dict has keys: signal_id, ticker, side, quantity, limit_price,
    rationale, sources.
    """
    if equity <= 0:
        logger.info("Equity is non-positive; returning no proposals.")
        return []

    if not signals:
        return []

    # Aggregate signals by ticker, preferring long over flat.
    ticker_signals: Dict[str, Dict[str, Any]] = {}
    for sig in signals:
        if "signal_id" not in sig:
            logger.warning("Skipping signal missing signal_id: %r", sig)
            continue
        if "direction" not in sig or sig["direction"] not in ("long", "flat"):
            logger.warning("Skipping signal with invalid direction: %r", sig)
            continue
        ticker = sig.get("ticker", "")
        if not ticker:
            logger.warning("Skipping signal with empty ticker: %r", sig)
            continue
        direction = sig["direction"]
        if ticker not in ticker_signals:
            ticker_signals[ticker] = sig
        else:
            existing_direction = ticker_signals[ticker]["direction"]
            if direction == "long" and existing_direction == "flat":
                ticker_signals[ticker] = sig
            # else keep existing (if both long or both flat, keep first)

    proposals = []
    for ticker, sig in ticker_signals.items():
        direction = sig["direction"]
        signal_id = sig["signal_id"]
        signal_type = sig.get("signal_type", "")
        rationale = sig.get("rationale", "")

        # Build prompt for model.
        prompt = (
            f"Propose a paper trade for {ticker}. Signal direction: {direction}. "
            f"Signal rationale: {rationale}. Current equity: {equity}. "
            f"Day start equity: {day_start_equity}. Orders today: {orders_today}. "
            "Return JSON with limit_price, quantity, rationale."
        )

        # Call model; fallback on failure or invalid result.
        limit_price: Optional[Decimal] = None
        quantity: Optional[Decimal] = None
        model_rationale: Optional[str] = None
        try:
            parsed, _resp = await generate_json(prompt, _RATIONALE_SCHEMA, role="model_general")
            # parsed should be dict with keys limit_price, quantity, rationale
            if isinstance(parsed, dict):
                raw_price = parsed.get("limit_price")
                raw_qty = parsed.get("quantity")
                raw_rationale = parsed.get("rationale")
                if raw_price is not None and raw_qty is not None:
                    try:
                        limit_price = Decimal(str(raw_price))
                        quantity = Decimal(str(raw_qty))
                        if limit_price <= 0 or quantity <= 0:
                            raise ValueError("non-positive")
                        model_rationale = str(raw_rationale)
                    except (InvalidOperation, ValueError, TypeError):
                        limit_price = None
                        quantity = None
                        model_rationale = None
        except Exception as exc:
            logger.warning("Model call failed, using fallback: %s", exc)

        if limit_price is None or quantity is None:
            # Fallback
            side = "buy" if direction == "long" else "sell"
            limit_price = FALLBACK_LIMIT_PRICE
            quantity = FALLBACK_NOTIONAL / limit_price
            model_rationale = (
                f"Fallback proposal for {direction} signal on {ticker}: "
                f"notional ${FALLBACK_NOTIONAL} at limit ${limit_price}."
            )
        else:
            side = "buy" if direction == "long" else "sell"
            # Ensure quantity positive even if model gave tiny positive; no change.
            # Ensure quantity not exceeding something? No requirement.
            pass

        proposals.append({
            "signal_id": signal_id,
            "ticker": ticker,
            "side": side,
            "quantity": str(quantity),
            "limit_price": str(limit_price),
            "rationale": model_rationale or rationale,
            "sources": [signal_type] if signal_type else [],
        })

    return proposals
