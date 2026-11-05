# tests/test_paper_p_l_calculator.py
"""Tests for the paper P&L calculator module using a real scratch database."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_p_l_calculator import (
    PnlRecord,
    _Position,
    _average_entry_price,
    _compute_positions,
    calculate_pnl,
)


def _fill(ticker: str, side: str, qty: str, price: str, ts: str) -> dict:
    return {
        "ticker": ticker,
        "side": side,
        "quantity": Decimal(qty),
        "price": Decimal(price),
        "filled_ts": datetime.fromisoformat(ts.replace("Z", "+00:00")),
    }


# --- Unit tests for internal helpers ---


def test_position_add_lot():
    pos = _Position(ticker="AAPL")
    pos.add_lot(Decimal("2"), Decimal("100"))
    assert pos.quantity == Decimal("2")
    assert pos.lots == [(Decimal("2"), Decimal("100"))]


def test_position_close_lot_full_fifo():
    pos = _Position(ticker="AAPL")
    pos.add_lot(Decimal("1"), Decimal("100"))
    pos.add_lot(Decimal("1"), Decimal("110"))
    pos.close_lot(Decimal("1"), Decimal("120"))
    assert pos.realized_pnl == Decimal("20")
    assert pos.quantity == Decimal("1")
    assert pos.lots == [(Decimal("1"), Decimal("110"))]


def test_position_close_lot_partial_fifo():
    pos = _Position(ticker="AAPL")
    pos.add_lot(Decimal("2"), Decimal("100"))
    pos.close_lot(Decimal("1"), Decimal("110"))
    assert pos.realized_pnl == Decimal("10")
    assert pos.quantity == Decimal("1")
    assert pos.lots == [(Decimal("1"), Decimal("100"))]


def test_position_close_lot_multiple_lots():
    pos = _Position(ticker="AAPL")
    pos.add_lot(Decimal("1"), Decimal("100"))
    pos.add_lot(Decimal("1"), Decimal("110"))
    pos.add_lot(Decimal("1"), Decimal("120"))
    pos.close_lot(Decimal("2"), Decimal("130"))
    assert pos.realized_pnl == Decimal("50")  # (130-100) + (130-110)
    assert pos.quantity == Decimal("1")
    assert pos.lots == [(Decimal("1"), Decimal("120"))]


def test_average_entry_price_empty():
    pos = _Position(ticker="AAPL")
    assert _average_entry_price(pos) is None


def test_average_entry_price_single_lot():
    pos = _Position(ticker="AAPL")
    pos.add_lot(Decimal("2"), Decimal("100"))
    assert _average_entry_price(pos) == Decimal("100")


def test_average_entry_price_multiple_lots():
    pos = _Position(ticker="AAPL")
    pos.add_lot(Decimal("1"), Decimal("100"))
    pos.add_lot(Decimal("3"), Decimal("200"))
    # (1*100 + 3*200) / 4 = 700/4 = 175
    assert _average_entry_price(pos) == Decimal("175")


def test_compute_positions_buy_sell():
    fills = [
        _fill("AAPL", "buy", "2", "100", "2026-10-23T20:00:00Z"),
        _fill("AAPL", "sell", "1", "120", "2026-10-23T20:01:00Z"),
    ]
    positions = _compute_positions(fills)
    assert "AAPL" in positions
    pos = positions["AAPL"]
    assert pos.quantity == Decimal("1")
    assert pos.realized_pnl == Decimal("20")
    assert pos.lots == [(Decimal("1"), Decimal("100"))]


def test_compute_positions_ignores_unknown_side():
    fills = [
        _fill("AAPL", "buy", "1", "100", "2026-10-23T20:00:00Z"),
        _fill("AAPL", "hold", "1", "100", "2026-10-23T20:01:00Z"),
    ]
    positions = _compute_positions(fills)
    assert positions["AAPL"].quantity == Decimal("1")


# --- Integration tests using real scratch database ---


@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    """Clear all relevant tables before each test."""
    async with async_session() as db:
        for table in [
            "fill",
            "broker_order_id",
            "approval",
            "proposal",
            "signal",
            "ingested_item",
            "pnl",
        ]:
            await db.execute(text(f"DELETE FROM {table}"))
        await db.commit()
    yield


async def insert_fill_chain(
    db,
    ticker: str,
    side: str,
    qty: str,
    price: str,
    filled_ts: datetime,
) -> None:
    """Insert a complete chain: ingested_item -> signal -> proposal -> approval -> broker_order_id -> fill."""
    external_id = uuid4().hex
    signal_type = "news_context"
    direction = "long"
    rationale = "test"
    operator_id = "test_operator"
    decision = "approved"
    broker_order_id_str = uuid4().hex
    fill_id = uuid4().hex

    # ingested_item
    ingested_result = await db.execute(
        text(
            """
            INSERT INTO ingested_item (source, external_id, published_ts, payload)
            VALUES ('news', :external_id, :published_ts, '{}'::json)
            RETURNING id
            """
        ),
        {"external_id": external_id, "published_ts": filled_ts},
    )
    ingested_id = ingested_result.scalar_one()

    # signal
    signal_result = await db.execute(
        text(
            """
            INSERT INTO signal (ingested_item_id, ticker, signal_type, direction, rationale)
            VALUES (:ingested_item_id, :ticker, :signal_type, :direction, :rationale)
            RETURNING id
            """
        ),
        {
            "ingested_item_id": ingested_id,
            "ticker": ticker,
            "signal_type": signal_type,
            "direction": direction,
            "rationale": rationale,
        },
    )
    signal_id = signal_result.scalar_one()

    # proposal
    proposal_result = await db.execute(
        text(
            """
            INSERT INTO proposal (signal_id, ticker, side, quantity, order_type, rationale)
            VALUES (:signal_id, :ticker, :side, :quantity, 'market', :rationale)
            RETURNING id
            """
        ),
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": side,
            "quantity": Decimal(qty),
            "rationale": rationale,
        },
    )
    proposal_id = proposal_result.scalar_one()

    # approval
    approval_result = await db.execute(
        text(
            """
            INSERT INTO approval (proposal_id, operator_id, decision)
            VALUES (:proposal_id, :operator_id, :decision)
            RETURNING id
            """
        ),
        {
            "proposal_id": proposal_id,
            "operator_id": operator_id,
            "decision": decision,
        },
    )
    approval_id = approval_result.scalar_one()

    # broker_order_id
    broker_result = await db.execute(
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
            "broker_order_id": broker_order_id_str,
        },
    )
    broker_id = broker_result.scalar_one()

    # fill
    await db.execute(
        text(
            """
            INSERT INTO fill (broker_order_id, fill_id, side, quantity, price, filled_ts)
            VALUES (:broker_order_id, :fill_id, :side, :quantity, :price, :filled_ts)
            """
        ),
        {
            "broker_order_id": broker_id,
            "fill_id": fill_id,
            "side": side,
            "quantity": Decimal(qty),
            "price": Decimal(price),
            "filled_ts": filled_ts,
        },
    )


@pytest.mark.asyncio
async def test_calculate_pnl_empty_fills():
    records = await calculate_pnl()
    assert records == []

    # Check that a pnl row was written with zero totals.
    async with async_session() as db:
        result = await db.execute(text("SELECT * FROM pnl"))
        row = result.fetchone()
        assert row is not None
        assert Decimal(str(row.realized_pnl)) == Decimal("0")
        assert Decimal(str(row.unrealized_pnl)) == Decimal("0")
        assert Decimal(str(row.equity)) == Decimal("0")


@pytest.mark.asyncio
async def test_calculate_pnl_single_open_position():
    """Two buys: 2@100 then 2@110 -> avg entry 105, last 110, open qty 4, unrealized 20."""
    async with async_session() as db:
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="buy",
            qty="2",
            price="100",
            filled_ts=datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc),
        )
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="buy",
            qty="2",
            price="110",
            filled_ts=datetime(2026, 10, 23, 20, 1, tzinfo=timezone.utc),
        )
        await db.commit()

    records = await calculate_pnl()
    assert len(records) == 1
    rec = records[0]
    assert rec.ticker == "AAPL"
    assert rec.realized_pnl == Decimal("0")
    assert rec.unrealized_pnl == Decimal("20")
    assert rec.open_quantity == Decimal("4")
    assert rec.average_entry_price == Decimal("105")
    assert rec.last_price == Decimal("110")


@pytest.mark.asyncio
async def test_calculate_pnl_multiple_tickers():
    """Ticker2 has three buys: 1@200, 1@200, 1@210 -> avg 203.333..., last 210, open 3, unrealized 20."""
    async with async_session() as db:
        # Ticker1: simple one buy
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="buy",
            qty="1",
            price="100",
            filled_ts=datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc),
        )
        # Ticker2: three buys
        await insert_fill_chain(
            db,
            ticker="MSFT",
            side="buy",
            qty="1",
            price="200",
            filled_ts=datetime(2026, 10, 23, 20, 1, tzinfo=timezone.utc),
        )
        await insert_fill_chain(
            db,
            ticker="MSFT",
            side="buy",
            qty="1",
            price="200",
            filled_ts=datetime(2026, 10, 23, 20, 2, tzinfo=timezone.utc),
        )
        await insert_fill_chain(
            db,
            ticker="MSFT",
            side="buy",
            qty="1",
            price="210",
            filled_ts=datetime(2026, 10, 23, 20, 3, tzinfo=timezone.utc),
        )
        await db.commit()

    records = await calculate_pnl()
    assert len(records) == 2
    by_ticker = {r.ticker: r for r in records}
    assert by_ticker["AAPL"].unrealized_pnl == Decimal("0")  # last price = entry
    assert by_ticker["MSFT"].unrealized_pnl == Decimal("20")
    assert by_ticker["MSFT"].open_quantity == Decimal("3")
    assert by_ticker["MSFT"].average_entry_price == Decimal("203.3333333333333333333333333")
    assert by_ticker["MSFT"].last_price == Decimal("210")


@pytest.mark.asyncio
async def test_calculate_pnl_closed_position():
    """Buy 2@100, sell 2@120 -> realized 40, unrealized 0, no open quantity."""
    async with async_session() as db:
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="buy",
            qty="2",
            price="100",
            filled_ts=datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc),
        )
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="sell",
            qty="2",
            price="120",
            filled_ts=datetime(2026, 10, 23, 20, 1, tzinfo=timezone.utc),
        )
        await db.commit()

    records = await calculate_pnl()
    assert len(records) == 1
    rec = records[0]
    assert rec.realized_pnl == Decimal("40")
    assert rec.unrealized_pnl == Decimal("0")
    assert rec.open_quantity == Decimal("0")
    assert rec.average_entry_price is None
    assert rec.last_price == Decimal("120")


@pytest.mark.asyncio
async def test_calculate_pnl_partial_close():
    """Buy 3@100, sell 1@130 -> realized 30, unrealized 60 (2*(130-100))."""
    async with async_session() as db:
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="buy",
            qty="3",
            price="100",
            filled_ts=datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc),
        )
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="sell",
            qty="1",
            price="130",
            filled_ts=datetime(2026, 10, 23, 20, 1, tzinfo=timezone.utc),
        )
        await db.commit()

    records = await calculate_pnl()
    rec = records[0]
    assert rec.realized_pnl == Decimal("30")
    assert rec.unrealized_pnl == Decimal("60")
    assert rec.open_quantity == Decimal("2")
    assert rec.last_price == Decimal("130")


@pytest.mark.asyncio
async def test_calculate_pnl_write_pnl_called_with_totals():
    """Buy 2@100, sell 1@120 -> realized 20, unrealized 20, equity 120."""
    async with async_session() as db:
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="buy",
            qty="2",
            price="100",
            filled_ts=datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc),
        )
        await insert_fill_chain(
            db,
            ticker="AAPL",
            side="sell",
            qty="1",
            price="120",
            filled_ts=datetime(2026, 10, 23, 20, 1, tzinfo=timezone.utc),
        )
        await db.commit()

    await calculate_pnl()

    async with async_session() as db:
        result = await db.execute(text("SELECT * FROM pnl"))
        row = result.fetchone()
        assert row is not None
        assert Decimal(str(row.realized_pnl)) == Decimal("20")
        assert Decimal(str(row.unrealized_pnl)) == Decimal("20")
        assert Decimal(str(row.equity)) == Decimal("120")
        assert Decimal(str(row.account_value)) == Decimal("120")
        assert Decimal(str(row.cash)) == Decimal("0")
