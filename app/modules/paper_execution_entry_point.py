# app/modules/paper_execution_entry_point.py
"""Paper-trading execution entry point.

Sends operator-approved proposals to Alpaca's paper account and records the
resulting broker order ids on the append-only audit trail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.modules.paper_alpaca_paper_client import AlpacaPaperClient
from app.modules.paper_audit_trail_writer import write_order

logger = logging.getLogger("scaffold")

# Fixed paper base URL (module constant, must never change at runtime).
PAPER_BASE_URL: str = "https://paper-api.alpaca.markets"

# Proposal-side and order-type values the proposal schema permits.
VALID_SIDES: frozenset[str] = frozenset({"buy", "sell"})
VALID_ORDER_TYPES: frozenset[str] = frozenset({"market", "limit"})
VALID_TIME_IN_FORCE: frozenset[str] = frozenset({"day", "gtc", "opg", "cls", "ioc", "fok"})


async def _send_order(client, proposal: dict[str, Any]) -> Any:
    """Send one approved proposal to the Alpaca paper client.

    The proposal's internal ``order_type`` is mapped to the documented
    ``type`` request field by passing it as ``order_type`` to
    :meth:`AlpacaPaperClient.submit_order`, which encodes it as ``type``
    in the request body sent to the paper API.
    """
    side = proposal.get("side")
    if side not in VALID_SIDES:
        raise ValueError(f"invalid side {side!r}")

    order_type = proposal.get("order_type")
    if order_type not in VALID_ORDER_TYPES:
        raise ValueError(f"invalid order_type {order_type!r}")

    time_in_force = proposal.get("time_in_force", "day")
    if time_in_force not in VALID_TIME_IN_FORCE:
        raise ValueError(f"invalid time_in_force {time_in_force!r}")

    try:
        quantity = Decimal(str(proposal.get("quantity", 0)))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValueError("quantity must be a positive number") from exc
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    kwargs: dict[str, Any] = {
        "symbol": proposal["ticker"],
        "qty": str(quantity),
        "side": side,
        "order_type": order_type,
        "time_in_force": time_in_force,
    }
    limit_price = proposal.get("limit_price")
    if limit_price is not None:
        kwargs["limit_price"] = str(limit_price)
    stop_price = proposal.get("stop_price")
    if stop_price is not None:
        kwargs["stop_price"] = str(stop_price)
    client_order_id = proposal.get("client_order_id")
    if client_order_id is not None:
        kwargs["client_order_id"] = client_order_id

    return await client.submit_order(**kwargs)


async def run_execution_entry_point(
    *,
    db,
    operator_id: str,
    timestamp: datetime,
) -> dict[str, Any]:
    """Send approved, not-yet-executed proposals and record broker order ids.

    Reads every approval with ``decision = 'approved'`` for ``operator_id``
    that has no ``broker_order_id`` row yet, sends each proposal to the
    Alpaca paper client, and writes the returned broker order id to the
    audit trail. Send failures are logged and skipped; the rest continue.
    """
    result = await db.execute(
        text(
            """
            SELECT a.id AS approval_id, a.proposal_id, p.ticker, p.side,
                   p.quantity, p.order_type, p.limit_price
            FROM approval a
            JOIN proposal p ON p.id = a.proposal_id
            WHERE a.decision = 'approved'
              AND a.operator_id = :operator_id
              AND NOT EXISTS (
                  SELECT 1 FROM broker_order_id boi
                  WHERE boi.approval_id = a.id
              )
            ORDER BY a.id
            """
        ),
        {"operator_id": operator_id},
    )
    rows = result.fetchall()

    client = AlpacaPaperClient()
    approvals = 0
    fills = 0
    orders = 0
    proposals = 0
    results: list[dict[str, Any]] = []
    try:
        for row in rows:
            proposals += 1
            proposal = {
                "ticker": row.ticker,
                "side": row.side,
                "quantity": str(row.quantity),
                "order_type": row.order_type,
                "limit_price": row.limit_price,
            }
            try:
                order = await _send_order(client, proposal)
            except Exception as exc:  # noqa: BLE001 - log and continue, like the other entry points
                logger.error(
                    "Failed to send order for approval %s: %s",
                    row.approval_id,
                    exc,
                )
                results.append(
                    {
                        "approval_id": row.approval_id,
                        "proposal_id": row.proposal_id,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                continue

            await write_order(
                approval_id=row.approval_id,
                proposal_id=row.proposal_id,
                broker_order_id=order.id,
            )
            orders += 1
            approvals += 1
            results.append(
                {
                    "approval_id": row.approval_id,
                    "proposal_id": row.proposal_id,
                    "status": "sent",
                    "broker_order_id": order.id,
                }
            )
    finally:
        await client.close()

    return {
        "approvals": approvals,
        "fills": fills,
        "orders": orders,
        "proposals": proposals,
        "results": results,
    }
