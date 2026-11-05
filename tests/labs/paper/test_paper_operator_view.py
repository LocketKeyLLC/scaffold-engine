# tests/test_paper_operator_view.py
"""Tests for the operator view module."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_operator_view import get_operator_view


async def _insert_ingested_item(db, *, source: str, external_id: str) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO ingested_item (source, external_id, published_ts, payload)
            VALUES (:source, :external_id, :published_ts, :payload)
            RETURNING id
            """
        ),
        {
            "source": source,
            "external_id": external_id,
            "published_ts": datetime.now(timezone.utc),
            "payload": "{}",
        },
    )
    return result.scalar_one()


async def _insert_signal(db, *, ingested_item_id: int, ticker: str) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO signal (ingested_item_id, ticker, signal_type, direction, rationale)
            VALUES (:ingested_item_id, :ticker, :signal_type, :direction, :rationale)
            RETURNING id
            """
        ),
        {
            "ingested_item_id": ingested_item_id,
            "ticker": ticker,
            "signal_type": "news_context",
            "direction": "long",
            "rationale": "test",
        },
    )
    return result.scalar_one()


async def _insert_proposal(
    db, *, signal_id: int, ticker: str, side: str = "buy", quantity: str = "1.5"
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO proposal (signal_id, ticker, side, quantity, order_type, limit_price, rationale)
            VALUES (:signal_id, :ticker, :side, :quantity, 'market', NULL, :rationale)
            RETURNING id
            """
        ),
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "rationale": "test rationale",
        },
    )
    return result.scalar_one()


async def _insert_approval(
    db, *, proposal_id: int, operator_id: str = "op", decision: str = "approved"
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO approval (proposal_id, operator_id, decision)
            VALUES (:proposal_id, :operator_id, :decision)
            RETURNING id
            """
        ),
        {"proposal_id": proposal_id, "operator_id": operator_id, "decision": decision},
    )
    return result.scalar_one()


async def _insert_broker_order_id(
    db, *, approval_id: int, proposal_id: int, broker_order_id: str
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO broker_order_id (approval_id, proposal_id, broker_order_id)
            VALUES (:approval_id, :proposal_id, :broker_order_id)
            RETURNING id
            """
        ),
        {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "broker_order_id": broker_order_id,
        },
    )
    return result.scalar_one()


async def _insert_fill(
    db,
    *,
    broker_order_id: int,
    fill_id: str,
    side: str,
    quantity: str,
    price: str,
    filled_ts: datetime,
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO fill (broker_order_id, fill_id, side, quantity, price, filled_ts)
            VALUES (:broker_order_id, :fill_id, :side, :quantity, :price, :filled_ts)
            RETURNING id
            """
        ),
        {
            "broker_order_id": broker_order_id,
            "fill_id": fill_id,
            "side": side,
            "quantity": quantity,
            "price": price,
            "filled_ts": filled_ts,
        },
    )
    return result.scalar_one()


async def _insert_pnl(
    db,
    *,
    as_of_ts: datetime,
    equity: str,
    spy_benchmark_return: str | None = None,
    cumulative_return: str | None = None,
    max_drawdown: str | None = None,
    sharpe_ratio: str | None = None,
    turnover: str | None = None,
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO pnl (as_of_ts, account_value, cash, equity, spy_benchmark_return,
                             cumulative_return, max_drawdown, sharpe_ratio, turnover)
            VALUES (:as_of_ts, :account_value, :cash, :equity, :spy_benchmark_return,
                    :cumulative_return, :max_drawdown, :sharpe_ratio, :turnover)
            RETURNING id
            """
        ),
        {
            "as_of_ts": as_of_ts,
            "account_value": equity,
            "cash": "0",
            "equity": equity,
            "spy_benchmark_return": spy_benchmark_return,
            "cumulative_return": cumulative_return,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe_ratio,
            "turnover": turnover,
        },
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_get_operator_view_empty_database():
    """Empty database returns empty lists and None metrics."""
    async with async_session() as db:
        view = await get_operator_view(db=db)
    assert view["pending_proposals"] == []
    assert view["open_positions"] == []
    assert view["equity_curve"] == []
    assert view["metrics"] == {
        "cumulative_return": None,
        "max_drawdown": None,
        "sharpe_ratio": None,
        "turnover": None,
    }


@pytest.mark.asyncio
async def test_pending_proposals():
    """A proposal without approval appears in pending proposals."""
    unique = uuid4().hex
    async with async_session() as db:
        # Insert proposal chain.
        ingested_id = await _insert_ingested_item(db, source="news", external_id=unique)
        signal_id = await _insert_signal(db, ingested_item_id=ingested_id, ticker="AAPL")
        proposal_id = await _insert_proposal(db, signal_id=signal_id, ticker="AAPL")

        view = await get_operator_view(db=db)

        assert len(view["pending_proposals"]) == 1
        pending = view["pending_proposals"][0]
        assert pending["id"] == proposal_id
        assert pending["ticker"] == "AAPL"
        assert pending["side"] == "buy"
        assert pending["quantity"] == Decimal("1.5")
        # no approval yet, so nothing else should appear
        assert view["open_positions"] == []


@pytest.mark.asyncio
async def test_proposal_with_approval_not_pending():
    """A proposal with an approval row is not pending."""
    unique = uuid4().hex
    async with async_session() as db:
        ingested_id = await _insert_ingested_item(db, source="news", external_id=unique)
        signal_id = await _insert_signal(db, ingested_item_id=ingested_id, ticker="MSFT")
        proposal_id = await _insert_proposal(db, signal_id=signal_id, ticker="MSFT")
        await _insert_approval(db, proposal_id=proposal_id, decision="approved")

        view = await get_operator_view(db=db)

        assert view["pending_proposals"] == []


@pytest.mark.asyncio
async def test_open_positions_from_fills():
    """Net quantity per ticker from fills is reported."""
    unique = uuid4().hex
    async with async_session() as db:
        # Build a full chain for ticker "SPY"
        ingested_id = await _insert_ingested_item(db, source="news", external_id=unique + "a")
        signal_id = await _insert_signal(db, ingested_item_id=ingested_id, ticker="SPY")
        proposal_id = await _insert_proposal(db, signal_id=signal_id, ticker="SPY", quantity="2")
        approval_id = await _insert_approval(db, proposal_id=proposal_id)
        broker_id = await _insert_broker_order_id(
            db, approval_id=approval_id, proposal_id=proposal_id, broker_order_id=unique + "b"
        )
        await _insert_fill(
            db,
            broker_order_id=broker_id,
            fill_id=unique + "f1",
            side="buy",
            quantity="2",
            price="500",
            filled_ts=datetime.now(timezone.utc),
        )

        view = await get_operator_view(db=db)

        assert len(view["open_positions"]) == 1
        pos = view["open_positions"][0]
        assert pos["ticker"] == "SPY"
        assert pos["net_qty"] == Decimal("2")


@pytest.mark.asyncio
async def test_open_positions_net_zero_excluded():
    """Net zero positions are not shown."""
    unique = uuid4().hex
    async with async_session() as db:
        ingested_id = await _insert_ingested_item(db, source="news", external_id=unique + "a")
        signal_id = await _insert_signal(db, ingested_item_id=ingested_id, ticker="QQQ")
        proposal_id = await _insert_proposal(db, signal_id=signal_id, ticker="QQQ", quantity="2")
        approval_id = await _insert_approval(db, proposal_id=proposal_id)
        broker_id = await _insert_broker_order_id(
            db, approval_id=approval_id, proposal_id=proposal_id, broker_order_id=unique + "b"
        )
        # Buy 2
        await _insert_fill(
            db,
            broker_order_id=broker_id,
            fill_id=unique + "f1",
            side="buy",
            quantity="2",
            price="300",
            filled_ts=datetime.now(timezone.utc),
        )
        # Sell 2
        await _insert_fill(
            db,
            broker_order_id=broker_id,
            fill_id=unique + "f2",
            side="sell",
            quantity="2",
            price="310",
            filled_ts=datetime.now(timezone.utc),
        )

        view = await get_operator_view(db=db)
        assert view["open_positions"] == []


@pytest.mark.asyncio
async def test_equity_curve_and_metrics():
    """PNL rows produce equity curve and metrics."""
    unique = uuid4().hex
    ts1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ts2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(
            db,
            as_of_ts=ts1,
            equity="10000",
            spy_benchmark_return="0.01",
            cumulative_return="0.02",
            max_drawdown="-0.05",
            sharpe_ratio="1.5",
            turnover="0.1",
        )
        await _insert_pnl(
            db,
            as_of_ts=ts2,
            equity="10200",
            spy_benchmark_return="0.03",
            cumulative_return="0.04",
            max_drawdown="-0.06",
            sharpe_ratio="1.7",
            turnover="0.2",
        )

        view = await get_operator_view(db=db)

        assert len(view["equity_curve"]) == 2
        assert view["equity_curve"][0]["as_of_ts"] == ts1
        assert view["equity_curve"][0]["equity"] == Decimal("10000")
        assert view["equity_curve"][0]["spy_benchmark_return"] == Decimal("0.01")
        assert view["equity_curve"][1]["as_of_ts"] == ts2
        assert view["equity_curve"][1]["equity"] == Decimal("10200")
        assert view["equity_curve"][1]["spy_benchmark_return"] == Decimal("0.03")

        metrics = view["metrics"]
        assert metrics["cumulative_return"] == Decimal("0.04")
        assert metrics["max_drawdown"] == Decimal("-0.06")
        assert metrics["sharpe_ratio"] == Decimal("1.7")
        assert metrics["turnover"] == Decimal("0.2")
