# tests/test_paper_daily_signal_proposal_batch_data.py
"""Tests for the daily signal-proposal batch data module."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_daily_signal_proposal_batch_data import (
    load_batch_context,
    load_batch_signals,
)


def _day_start_ts(trading_day: date) -> datetime:
    return datetime.combine(trading_day, datetime.min.time(), tzinfo=timezone.utc)


async def _insert_ingested_item(
    db,
    *,
    source: str,
    external_id: str,
    published_ts: datetime,
    payload: dict,
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO ingested_item
                (source, external_id, published_ts, payload)
            VALUES
                (:source, :external_id, :published_ts, CAST(:payload AS json))
            RETURNING id
            """
        ),
        {
            "source": source,
            "external_id": external_id,
            "published_ts": published_ts,
            "payload": __import__("json").dumps(payload),
        },
    )
    return result.scalar_one()


async def _insert_signal(
    db,
    *,
    ingested_item_id: int,
    ticker: str,
    signal_type: str,
    direction: str,
    rationale: str,
) -> int:
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
    return result.scalar_one()


async def _insert_proposal(
    db,
    *,
    signal_id: int,
    ticker: str,
    side: str,
    quantity: str,
    order_type: str,
    rationale: str,
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO proposal
                (signal_id, ticker, side, quantity, order_type, rationale)
            VALUES
                (:signal_id, :ticker, :side, :quantity, :order_type, :rationale)
            RETURNING id
            """
        ),
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": side,
            "quantity": Decimal(quantity),
            "order_type": order_type,
            "rationale": rationale,
        },
    )
    return result.scalar_one()


async def _insert_approval(
    db,
    *,
    proposal_id: int,
    operator_id: str,
    decision: str,
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO approval
                (proposal_id, operator_id, decision)
            VALUES
                (:proposal_id, :operator_id, :decision)
            RETURNING id
            """
        ),
        {
            "proposal_id": proposal_id,
            "operator_id": operator_id,
            "decision": decision,
        },
    )
    return result.scalar_one()


async def _insert_broker_order_id(
    db,
    *,
    approval_id: int,
    proposal_id: int,
    broker_order_id: str,
    submitted_ts: datetime,
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO broker_order_id
                (approval_id, proposal_id, broker_order_id, submitted_ts)
            VALUES
                (:approval_id, :proposal_id, :broker_order_id, :submitted_ts)
            RETURNING id
            """
        ),
        {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "broker_order_id": broker_order_id,
            "submitted_ts": submitted_ts,
        },
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_load_batch_context_no_equity_no_orders():
    """A fresh day with no equity record and no orders returns None and 0."""
    trading_day = date(2026, 10, 25)
    timestamp = _day_start_ts(trading_day) + timedelta(hours=1)

    async with async_session() as db:
        context = await load_batch_context(
            timestamp=timestamp,
            trading_day=trading_day,
            db=db,
        )

    assert context["day_start_equity"] is None
    assert context["orders_today"] == 0


@pytest.mark.asyncio
async def test_load_batch_context_reads_day_start_equity():
    """The day-start equity record for the trading day is returned."""
    trading_day = date(2026, 10, 25)
    timestamp = _day_start_ts(trading_day) + timedelta(hours=1)

    async with async_session() as db:
        from app.modules.paper_day_start_equity_recording import (
            record_day_start_equity,
        )

        await record_day_start_equity(
            db=db,
            equity=Decimal("100000.00"),
            as_of_ts=_day_start_ts(trading_day),
        )
        await db.commit()

        context = await load_batch_context(
            timestamp=timestamp,
            trading_day=trading_day,
            db=db,
        )

    assert context["day_start_equity"] == Decimal("100000.00")
    assert context["orders_today"] == 0


@pytest.mark.asyncio
async def test_load_batch_context_counts_orders_today_only():
    """Orders placed today are counted; yesterday's are not."""
    trading_day = date(2026, 10, 25)
    yesterday = trading_day - timedelta(days=1)
    timestamp = _day_start_ts(trading_day) + timedelta(hours=1)

    async with async_session() as db:
        # Build a full chain for yesterday's order.
        item_id = await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=_day_start_ts(yesterday),
            payload={"ticker": "SPY"},
        )
        signal_id = await _insert_signal(
            db,
            ingested_item_id=item_id,
            ticker="SPY",
            signal_type="news_context",
            direction="long",
            rationale="test",
        )
        proposal_id = await _insert_proposal(
            db,
            signal_id=signal_id,
            ticker="SPY",
            side="buy",
            quantity="1",
            order_type="market",
            rationale="test",
        )
        approval_id = await _insert_approval(
            db,
            proposal_id=proposal_id,
            operator_id="test-operator",
            decision="approved",
        )
        await _insert_broker_order_id(
            db,
            approval_id=approval_id,
            proposal_id=proposal_id,
            broker_order_id=uuid4().hex,
            submitted_ts=_day_start_ts(yesterday) + timedelta(hours=2),
        )

        # Build a full chain for today's order.
        item_id2 = await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=_day_start_ts(trading_day),
            payload={"ticker": "VOO"},
        )
        signal_id2 = await _insert_signal(
            db,
            ingested_item_id=item_id2,
            ticker="VOO",
            signal_type="news_context",
            direction="long",
            rationale="test",
        )
        proposal_id2 = await _insert_proposal(
            db,
            signal_id=signal_id2,
            ticker="VOO",
            side="buy",
            quantity="1",
            order_type="market",
            rationale="test",
        )
        approval_id2 = await _insert_approval(
            db,
            proposal_id=proposal_id2,
            operator_id="test-operator",
            decision="approved",
        )
        await _insert_broker_order_id(
            db,
            approval_id=approval_id2,
            proposal_id=proposal_id2,
            broker_order_id=uuid4().hex,
            submitted_ts=_day_start_ts(trading_day) + timedelta(hours=1),
        )

        await db.commit()

        context = await load_batch_context(
            timestamp=timestamp,
            trading_day=trading_day,
            db=db,
        )

    assert context["orders_today"] == 1


@pytest.mark.asyncio
async def test_load_batch_context_uses_own_session_when_db_none():
    """When db is None, the function opens and closes its own session."""
    trading_day = date(2026, 10, 25)
    timestamp = _day_start_ts(trading_day) + timedelta(hours=1)

    context = await load_batch_context(
        timestamp=timestamp,
        trading_day=trading_day,
    )

    assert context["day_start_equity"] is None
    assert context["orders_today"] == 0


@pytest.mark.asyncio
async def test_load_batch_signals_returns_generated_signals(monkeypatch):
    """load_batch_signals delegates to paper_signal_generator.generate_signals."""
    timestamp = datetime(2026, 10, 25, 14, 0, tzinfo=timezone.utc)
    expected = [{"ticker": "SPY", "signal_type": "news_context"}]

    async def fake_generate_signals(ts, *, db=None):
        assert ts == timestamp
        return expected

    monkeypatch.setattr(
        "app.modules.paper_daily_signal_proposal_batch_data.generate_signals",
        fake_generate_signals,
    )

    result = await load_batch_signals(timestamp=timestamp)

    assert result == expected


@pytest.mark.asyncio
async def test_load_batch_signals_passes_db_through(monkeypatch):
    """load_batch_signals forwards the db argument to generate_signals."""
    timestamp = datetime(2026, 10, 25, 14, 0, tzinfo=timezone.utc)
    sentinel_db = object()

    async def fake_generate_signals(ts, *, db=None):
        assert db is sentinel_db
        return []

    monkeypatch.setattr(
        "app.modules.paper_daily_signal_proposal_batch_data.generate_signals",
        fake_generate_signals,
    )

    result = await load_batch_signals(timestamp=timestamp, db=sentinel_db)

    assert result == []
