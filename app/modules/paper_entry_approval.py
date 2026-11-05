# app/modules/paper_entry_approval.py
"""Paper trading lab approval entry point.

Runs the end-to-end approval path: generate signals, propose trades,
run risk checks, request operator approval, and write the approval
decision to the audit trail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_approval
from app.modules.paper_operator_approval_gate import request_approval
from app.modules.paper_risk_control_module import evaluate_proposal
from app.modules.paper_signal_generator import generate_signals
from app.modules.paper_trade_proposer import propose_trades

logger = logging.getLogger("scaffold")


async def run_approval_entry_point(
    *,
    db: Optional[Any] = None,
    timestamp: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run the approval path end to end.

    Generates signals from ingested items, proposes trades from those
    signals, evaluates each proposal against the deterministic risk
    controls, requests operator approval for proposals that pass all
    risk checks, and writes each approval decision to the audit trail.

    Returns a summary dict with counts and per-proposal results.
    """
    own_db = db is None
    if own_db:
        db = async_session()
    try:
        now = timestamp or datetime.now(timezone.utc)

        # 1. Generate signals from ingested items.
        signals = await generate_signals(now, db=db)
        if not signals:
            logger.info("No signals generated; approval path complete.")
            return {"signals": 0, "proposals": 0, "approvals": 0, "results": []}

        # 2. Propose trades from signals. The proposer applies risk
        #    limits internally (position size, daily loss, order count)
        #    and returns only proposals that are eligible.
        proposals = await propose_trades(
            signals,
            equity=None,
            day_start_equity=None,
            orders_today=0,
            db=db,
        )
        if not proposals:
            logger.info("No proposals generated; approval path complete.")
            return {
                "signals": len(signals),
                "proposals": 0,
                "approvals": 0,
                "results": [],
            }

        results: list[dict[str, Any]] = []
        approvals = 0

        for proposal in proposals:
            proposal_id = proposal.get("id")
            ticker = proposal.get("ticker")
            side = proposal.get("side")
            quantity = proposal.get("quantity")
            order_type = proposal.get("order_type", "market")
            limit_price = proposal.get("limit_price")
            rationale = proposal.get("rationale", "")

            # 3. Re-evaluate risk controls per proposal (deterministic).
            risk_decision = evaluate_proposal(
                symbol=ticker,
                equity=proposal.get("equity", 0),
                day_start_equity=proposal.get("day_start_equity", 0),
                orders_today=proposal.get("orders_today", 0),
                proposed_notional=proposal.get("proposed_notional", 0),
                current_position_notional=proposal.get(
                    "current_position_notional", 0
                ),
            )
            if not risk_decision.allowed:
                logger.info(
                    "Proposal %s rejected by risk controls: %s",
                    proposal_id,
                    risk_decision.reason,
                )
                results.append(
                    {
                        "proposal_id": proposal_id,
                        "decision": "rejected",
                        "reason": risk_decision.reason,
                    }
                )
                continue

            # 4. Request operator approval.
            approval = await request_approval(proposal, db=db)
            decision = approval.get("decision", "rejected")
            reason = approval.get("reason", "")

            # 5. Write the approval decision to the audit trail.
            await write_approval(
                proposal_id=proposal_id,
                operator_id=approval.get("operator_id"),
                decision=decision,
                reason=reason,
            )

            if decision == "approved":
                approvals += 1

            results.append(
                {
                    "proposal_id": proposal_id,
                    "decision": decision,
                    "reason": reason,
                }
            )

        return {
            "signals": len(signals),
            "proposals": len(proposals),
            "approvals": approvals,
            "results": results,
        }
    finally:
        if own_db:
            await db.close()


__all__ = ["run_approval_entry_point"]
