# app/modules/paper_daily_signal_proposal_batch.py
"""Daily batch entry points for the paper trading pipeline.

Runs the signal-to-proposal stage once daily before US market open and
on demand. Reads signals via the data layer, applies deterministic sizing
and risk limits through `propose_trades`, and writes each eligible
proposal plus a passed risk check to the append-only audit trail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.database import async_session
from app.modules.paper_daily_signal_proposal_batch_data import (
    load_batch_context,
    load_batch_signals,
)
from app.modules.paper_trade_proposer import propose_trades
from app.modules.paper_audit_trail_writer import write_proposal, write_risk_check

logger = logging.getLogger("scaffold")


async def _run_batch(
    *,
    db: Optional[Any] = None,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """Shared implementation for daily and on-demand batch runs."""
    own_db = db is None
    if own_db:
        db = async_session()
    try:
        now = timestamp or datetime.now(timezone.utc)
        trading_day = now.date()

        context = await load_batch_context(
            timestamp=now, trading_day=trading_day, db=db
        )
        day_start_equity = context["day_start_equity"]
        orders_today = context["orders_today"]

        if day_start_equity is None:
            logger.warning(
                "No day-start equity recorded for %s; batch run aborted.",
                trading_day,
            )
            return {
                "signals": 0,
                "proposals": 0,
                "proposals_written": 0,
                "risk_checks_written": 0,
                "results": [],
            }

        signals = await load_batch_signals(timestamp=now, db=db)
        if not signals:
            return {
                "signals": 0,
                "proposals": 0,
                "proposals_written": 0,
                "risk_checks_written": 0,
                "results": [],
            }

        proposals = await propose_trades(
            signals,
            equity=day_start_equity,
            day_start_equity=day_start_equity,
            orders_today=orders_today,
            db=db,
        )

        results: list[dict[str, Any]] = []
        proposals_written = 0
        risk_checks_written = 0

        for proposal in proposals:
            proposal_id = await write_proposal(
                signal_id=proposal["signal_id"],
                ticker=proposal["ticker"],
                side=proposal["side"],
                quantity=proposal["quantity"],
                order_type=proposal.get("order_type", "market"),
                limit_price=proposal.get("limit_price"),
                rationale=proposal["rationale"],
            )
            await write_risk_check(
                proposal_id=proposal_id,
                check_type="batch_deterministic",
                passed=True,
                reason="Passed deterministic risk controls",
                metrics={},
            )
            proposals_written += 1
            risk_checks_written += 1
            results.append(
                {
                    "proposal_id": proposal_id,
                    "ticker": proposal["ticker"],
                    "side": proposal["side"],
                    "quantity": proposal["quantity"],
                }
            )

        return {
            "signals": len(signals),
            "proposals": len(proposals),
            "proposals_written": proposals_written,
            "risk_checks_written": risk_checks_written,
            "results": results,
        }
    finally:
        if own_db:
            await db.close()


async def run_daily_batch(
    *,
    db: Optional[Any] = None,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run the daily signal-to-proposal batch.

    Intended to be invoked once per trading day before US market open.
    """
    return await _run_batch(db=db, timestamp=timestamp)


async def run_on_demand_batch(
    *,
    db: Optional[Any] = None,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run the signal-to-proposal batch on demand.

    Suitable for manual or test invocations at any time.
    """
    return await _run_batch(db=db, timestamp=timestamp)


__all__ = ["run_daily_batch", "run_on_demand_batch"]
