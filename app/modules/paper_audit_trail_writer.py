# app/modules/paper_audit_trail_writer.py
"""Append-only writers for the paper trading audit trail."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold")


def _now() -> datetime:
    """Current UTC timestamp with timezone info."""
    return datetime.now(timezone.utc)


async def write_signal(
    *,
    ingested_item_id: int,
    ticker: str,
    signal_type: str,
    direction: str,
    rationale: str,
) -> int:
    """Insert a signal row and return its id."""
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO signal
                    (ingested_item_id, ticker, signal_type, direction, rationale)
                VALUES
                    (:ingested_item_id, :ticker, :signal_type, :direction, :rationale)
                RETURNING id
                """
            ),
            {
                "ingested_item_id": ingested_item_id,
                "ticker": ticker,
                "signal_type": signal_type,
                "direction": direction,
                "rationale": rationale,
            },
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO signal returned no row")
        signal_id = row[0]
        await db.commit()
    return signal_id


async def write_proposal(
    *,
    signal_id: int,
    ticker: str,
    side: str,
    quantity: str,
    order_type: str,
    limit_price: str | None,
    rationale: str,
) -> int:
    """Insert a proposal row and return its id."""
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO proposal
                    (signal_id, ticker, side, quantity, order_type, limit_price, rationale)
                VALUES
                    (:signal_id, :ticker, :side, :quantity, :order_type, :limit_price, :rationale)
                RETURNING id
                """
            ),
            {
                "signal_id": signal_id,
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "order_type": order_type,
                "limit_price": limit_price,
                "rationale": rationale,
            },
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO proposal returned no row")
        proposal_id = row[0]
        await db.commit()
    return proposal_id


async def write_risk_check(
    *,
    proposal_id: int,
    check_type: str,
    passed: bool,
    reason: str | None,
    metrics: dict,
) -> int:
    """Insert a risk_check row and return its id."""
    metrics_json = json.dumps(metrics)
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO risk_check
                    (proposal_id, check_type, passed, reason, metrics)
                VALUES
                    (:proposal_id, :check_type, :passed, :reason, CAST(:metrics AS json))
                RETURNING id
                """
            ),
            {
                "proposal_id": proposal_id,
                "check_type": check_type,
                "passed": passed,
                "reason": reason,
                "metrics": metrics_json,
            },
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO risk_check returned no row")
        risk_check_id = row[0]
        await db.commit()
    return risk_check_id


async def write_approval(
    *,
    proposal_id: int,
    operator_id: str,
    decision: str,
    reason: str | None,
) -> int:
    """Insert an approval row and return its id."""
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO approval
                    (proposal_id, operator_id, decision, reason)
                VALUES
                    (:proposal_id, :operator_id, :decision, :reason)
                RETURNING id
                """
            ),
            {
                "proposal_id": proposal_id,
                "operator_id": operator_id,
                "decision": decision,
                "reason": reason,
            },
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO approval returned no row")
        approval_id = row[0]
        await db.commit()
    return approval_id


async def write_order(
    *,
    approval_id: int,
    proposal_id: int,
    broker_order_id: str,
) -> int:
    """Insert a broker_order_id row and return its id."""
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO broker_order_id
                    (approval_id, proposal_id, broker_order_id)
                VALUES
                    (:approval_id, :proposal_id, :broker_order_id)
                RETURNING id
                """
            ),
            {
                "approval_id": approval_id,
                "proposal_id": proposal_id,
                "broker_order_id": broker_order_id,
            },
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO broker_order_id returned no row")
        order_id = row[0]
        await db.commit()
    return order_id


async def write_fill(
    *,
    broker_order_id: int,
    fill_id: str,
    side: str,
    quantity: str,
    price: str,
    commission: str,
    filled_ts: datetime,
) -> int:
    """Insert a fill row and return its id."""
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO fill
                    (broker_order_id, fill_id, side, quantity, price, commission, filled_ts)
                VALUES
                    (:broker_order_id, :fill_id, :side, :quantity, :price, :commission, :filled_ts)
                RETURNING id
                """
            ),
            {
                "broker_order_id": broker_order_id,
                "fill_id": fill_id,
                "side": side,
                "quantity": quantity,
                "price": price,
                "commission": commission,
                "filled_ts": filled_ts,
            },
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO fill returned no row")
        fill_id_db = row[0]
        await db.commit()
    return fill_id_db


async def write_pnl(
    *,
    as_of_ts: datetime,
    account_value: str,
    cash: str,
    equity: str,
    realized_pnl: str,
    unrealized_pnl: str,
    daily_return: str | None,
    cumulative_return: str | None,
    max_drawdown: str | None,
    sharpe_ratio: str | None,
    turnover: str | None,
    spy_benchmark_return: str | None,
) -> int:
    """Insert a pnl row and return its id."""
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                INSERT INTO pnl
                    (as_of_ts, account_value, cash, equity, realized_pnl, unrealized_pnl,
                     daily_return, cumulative_return, max_drawdown, sharpe_ratio, turnover,
                     spy_benchmark_return)
                VALUES
                    (:as_of_ts, :account_value, :cash, :equity, :realized_pnl, :unrealized_pnl,
                     :daily_return, :cumulative_return, :max_drawdown, :sharpe_ratio, :turnover,
                     :spy_benchmark_return)
                RETURNING id
                """
            ),
            {
                "as_of_ts": as_of_ts,
                "account_value": account_value,
                "cash": cash,
                "equity": equity,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "daily_return": daily_return,
                "cumulative_return": cumulative_return,
                "max_drawdown": max_drawdown,
                "sharpe_ratio": sharpe_ratio,
                "turnover": turnover,
                "spy_benchmark_return": spy_benchmark_return,
            },
        )
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO pnl returned no row")
        pnl_id = row[0]
        await db.commit()
    return pnl_id
