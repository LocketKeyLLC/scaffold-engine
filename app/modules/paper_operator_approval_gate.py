# app/modules/paper_operator_approval_gate.py
"""Operator approval gate for paper-trading proposals."""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text

from app.database import async_session
from app.modules.operator_ask import answer_handler, decision, open_decision_job

logger = logging.getLogger("scaffold")

APPROVAL_MODULE = "app.modules.paper_operator_approval_gate"


def _json_safe(value: Any) -> Any:
    """Recursively convert non-JSON-native types to JSON-safe equivalents."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _format_decimal(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _proposal_display(proposal: dict[str, Any]) -> str:
    symbol = proposal.get("symbol", "?")
    side = proposal.get("side", "?")
    quantity = _format_decimal(proposal.get("quantity"))
    limit_price = _format_decimal(proposal.get("limit_price"))
    rationale = proposal.get("rationale", "").strip() or "(no rationale)"
    risk = proposal.get("risk_check", {})
    risk_allowed = risk.get("allowed", False)
    risk_reason = risk.get("reason") or "passed"
    return (
        f"Symbol: {symbol}\n"
        f"Side: {side}\n"
        f"Quantity: {quantity}\n"
        f"Limit price: {limit_price}\n"
        f"Risk check: {'allowed' if risk_allowed else 'rejected'} — {risk_reason}\n"
        f"Rationale: {rationale}"
    )


async def _record_rejection(db: Any, proposal: dict[str, Any], reason: str) -> None:
    await db.execute(
        text(
            """
            INSERT INTO approval (proposal_id, operator_id, decision, reason)
            VALUES (:proposal_id, :operator_id, :decision, :reason)
            """
        ),
        {
            "proposal_id": proposal.get("proposal_id"),
            "operator_id": "operator",
            "decision": "rejected",
            "reason": reason,
        },
    )
    await db.commit()


async def _record_approval(
    db: Any,
    *,
    proposal_id: int,
    operator_id: str,
    decision: str,
    reason: Optional[str] = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO approval (proposal_id, operator_id, decision, reason)
            VALUES (:proposal_id, :operator_id, :decision, :reason)
            """
        ),
        {
            "proposal_id": proposal_id,
            "operator_id": operator_id,
            "decision": decision,
            "reason": reason,
        },
    )
    await db.commit()


async def request_approval(proposal: dict[str, Any], *, db: Optional[Any] = None) -> dict[str, Any]:
    """Open a decision job for one proposal and return its approval dict.

    The returned dict carries the proposal payload and the job id; the
    operator's answer arrives later via the @answer_handler.
    """
    owns_db = db is None
    if db is None:
        db = async_session()

    try:
        proposal_id = proposal.get("proposal_id")
        key = f"approve_proposal_{proposal_id}"
        question = "Approve this paper-trading proposal?"
        detail = _proposal_display(proposal)

        # Build a JSON-safe copy for the decision payload.
        serializable_proposal = _json_safe(proposal)

        decisions = [
            decision(
                key=key,
                question=question,
                detail=detail,
                options=["Approve", "Reject"],
                payload={
                    "proposal": serializable_proposal,
                    "proposal_id": proposal_id,
                },
            )
        ]

        job_id = await open_decision_job(
            db,
            title=f"Paper proposal {proposal_id}",
            decisions=decisions,
            owner="operator",
            on_answer=f"{APPROVAL_MODULE}:handle_approval_answer",
        )

        return {
            "job_id": job_id,
            "proposal_id": proposal_id,
            "status": "pending",
            "proposal": proposal,
        }
    finally:
        if owns_db:
            await db.close()


@answer_handler
async def handle_approval_answer(
    job_id: str,
    node_key: str,
    answer: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    """Handle an operator answer for a paper proposal.

    Runs when the operator answers in the UI. Only acts when approved.
    """
    if not answer.get("approved"):
        proposal = payload.get("proposal", {})
        reason = answer.get("note") or "rejected by operator"
        async with async_session() as db:
            await _record_rejection(db, proposal, reason)
        logger.info("Proposal %s rejected by operator: %s", payload.get("proposal_id"), reason)
        return

    proposal_id = payload.get("proposal_id")
    operator_id = answer.get("choice", "operator")
    async with async_session() as db:
        await _record_approval(
            db,
            proposal_id=proposal_id,
            operator_id=operator_id,
            decision="approved",
        )
    logger.info("Proposal %s approved by operator", proposal_id)


__all__ = [
    "APPROVAL_MODULE",
    "handle_approval_answer",
    "request_approval",
]
