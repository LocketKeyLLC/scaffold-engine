# app/modules/paper_order_executor.py
"""Execute an approved paper order through the Alpaca paper client.

The module receives an approval dict (as stored in the approval table), loads
the matching proposal, maps the proposal's order_type to the Alpaca request
field `type` (the client's `submit_order` already sends it as `type`), submits
the order, and records the broker order id in the audit trail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_alpaca_paper_client import (
    AlpacaOrder,
    AlpacaPaperClient,
    AlpacaPaperClientError,
)
from app.modules.paper_audit_trail_writer import write_order

logger = logging.getLogger("scaffold")


async def execute_order(approval: dict[str, Any]) -> dict[str, Any]:
    """Execute the approved proposal associated with the given approval.

    Args:
        approval: A dict containing at least the keys `id`, `proposal_id`,
            and `decision`. `decision` must be `'approved'` for an order to
            be submitted.

    Returns:
        A dict with keys:
            - `broker_order_id`: the database id of the inserted
              `broker_order_id` row.
            - `order`: the `AlpacaOrder` object returned by the client.
            - `proposal_id`: the proposal id the order was created for.
            - `status`: `'submitted'` on success, `'rejected'` if the
              approval decision is not `'approved'`.

    Raises:
        KeyError: If any of `id`, `proposal_id`, or `decision` is missing
            from `approval`.
        ValueError: If the proposal referenced by `proposal_id` does not
            exist, or if the approval decision is not `'approved'`.
        AlpacaPaperClientError: If the Alpaca client fails to submit the
            order.
        Exception: Any exception raised by `paper_audit_trail_writer.write_order`
            is propagated.
    """
    required = ("id", "proposal_id", "decision")
    for key in required:
        if key not in approval:
            raise KeyError(f"approval dict is missing required key {key!r}")

    if approval["decision"] != "approved":
        logger.info(
            "Approval %s is not approved (decision=%s); no order will be sent.",
            approval["id"],
            approval["decision"],
        )
        return {
            "broker_order_id": None,
            "order": None,
            "proposal_id": approval["proposal_id"],
            "status": "rejected",
        }

    proposal_id = approval["proposal_id"]

    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT ticker, side, quantity, order_type, limit_price
                FROM proposal
                WHERE id = :proposal_id
                """
            ),
            {"proposal_id": proposal_id},
        )
        row = result.fetchone()
        if row is None:
            raise ValueError(f"Proposal {proposal_id} not found")

        ticker, side, quantity, order_type, limit_price = row
        qty = Decimal(str(quantity))
        limit = Decimal(str(limit_price)) if limit_price is not None else None

        client = AlpacaPaperClient()
        try:
            order: AlpacaOrder = await client.submit_order(
                symbol=ticker,
                qty=qty,
                side=side,
                order_type=order_type,  # client maps this to the Alpaca `type` field
                time_in_force="day",
                limit_price=limit,
            )
        finally:
            await client.close()

        # Record the broker order id in the audit trail.
        broker_order_row_id = await write_order(
            approval_id=approval["id"],
            proposal_id=proposal_id,
            broker_order_id=order.id,
        )

        return {
            "broker_order_id": broker_order_row_id,
            "order": order,
            "proposal_id": proposal_id,
            "status": "submitted",
        }
