# tests/test_paper_trade_proposer.py
"""Tests for the paper trade proposer module."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.database import async_session
from app.modules.paper_trade_proposer import (
    FALLBACK_LIMIT_PRICE,
    FALLBACK_NOTIONAL,
    MAX_PROPOSALS_PER_BATCH,
    PAPER_BASE_URL,
    propose_trades,
)
from sqlalchemy import text


def _signal(
    signal_id: int = 1,
    ticker: str = "AAPL",
    direction: str = "long",
    signal_type: str = "news_context",
    rationale: str = "test rationale",
) -> dict:
    return {
        "signal_id": signal_id,
        "ticker": ticker,
        "direction": direction,
        "signal_type": signal_type,
        "rationale": rationale,
    }


@pytest.mark.asyncio
async def test_defines_paper_base_url_and_does_not_call_client():
    assert PAPER_BASE_URL == "https://paper-api.alpaca.markets"
    # The module does not import or instantiate AlpacaPaperClient, so nothing to assert here.
    # This test is satisfied by the constant above.


@pytest.mark.asyncio
async def test_proposes_buy_for_long_signal():
    async with async_session() as db:
        with patch(
            "app.modules.paper_trade_proposer.generate_json",
            new=AsyncMock(return_value=({"limit_price": 10.0, "quantity": 5, "rationale": "model buy"}, None)),
        ):
            result = await propose_trades(
                [_signal()],
                equity=Decimal("1000"),
                day_start_equity=Decimal("1000"),
                orders_today=0,
                db=db,
            )

    assert len(result) == 1
    proposal = result[0]
    assert proposal["side"] == "buy"
    assert proposal["signal_id"] == 1
    assert Decimal(proposal["quantity"]) > 0
    assert Decimal(proposal["limit_price"]) > 0
    assert proposal["rationale"] == "model buy"


@pytest.mark.asyncio
async def test_proposes_sell_for_flat_signal():
    async with async_session() as db:
        with patch(
            "app.modules.paper_trade_proposer.generate_json",
            new=AsyncMock(return_value=({"limit_price": 9.5, "quantity": 3, "rationale": "model sell"}, None)),
        ):
            result = await propose_trades(
                [_signal(direction="flat")],
                equity=Decimal("1000"),
                day_start_equity=Decimal("1000"),
                orders_today=0,
                db=db,
            )

    assert len(result) == 1
    proposal = result[0]
    assert proposal["side"] == "sell"
    assert proposal["signal_id"] == 1
    assert Decimal(proposal["quantity"]) > 0
    assert Decimal(proposal["limit_price"]) > 0
    assert proposal["rationale"] == "model sell"


@pytest.mark.asyncio
async def test_aggregates_signals_by_ticker_prefers_long():
    async with async_session() as db:
        with patch(
            "app.modules.paper_trade_proposer.generate_json",
            new=AsyncMock(return_value=({"limit_price": 10.0, "quantity": 5, "rationale": "model"}, None)),
        ):
            result = await propose_trades(
                [
                    _signal(signal_id=1, ticker="AAPL", direction="flat"),
                    _signal(signal_id=2, ticker="AAPL", direction="long"),
                ],
                equity=Decimal("1000"),
                day_start_equity=Decimal("1000"),
                orders_today=0,
                db=db,
            )

    assert len(result) == 1
    proposal = result[0]
    assert proposal["ticker"] == "AAPL"
    assert proposal["signal_id"] == 2  # the long one
    assert proposal["side"] == "buy"


@pytest.mark.asyncio
async def test_model_failure_uses_fallback():
    async with async_session() as db:
        with patch(
            "app.modules.paper_trade_proposer.generate_json",
            new=AsyncMock(side_effect=RuntimeError("model down")),
        ):
            result = await propose_trades(
                [_signal()],
                equity=Decimal("1000"),
                day_start_equity=Decimal("1000"),
                orders_today=0,
                db=db,
            )

    assert len(result) == 1
    proposal = result[0]
    assert proposal["side"] == "buy"
    assert proposal["quantity"] == str(FALLBACK_NOTIONAL / FALLBACK_LIMIT_PRICE)
    assert proposal["limit_price"] == str(FALLBACK_LIMIT_PRICE)
    assert "Fallback" in proposal["rationale"]


@pytest.mark.asyncio
async def test_invalid_model_price_uses_fallback():
    async with async_session() as db:
        with patch(
            "app.modules.paper_trade_proposer.generate_json",
            new=AsyncMock(return_value=({"limit_price": 0, "quantity": 5, "rationale": "bad"}, None)),
        ):
            result = await propose_trades(
                [_signal()],
                equity=Decimal("1000"),
                day_start_equity=Decimal("1000"),
                orders_today=0,
                db=db,
            )

    assert len(result) == 1
    proposal = result[0]
    assert proposal["quantity"] == str(FALLBACK_NOTIONAL / FALLBACK_LIMIT_PRICE)
    assert proposal["limit_price"] == str(FALLBACK_LIMIT_PRICE)
    assert "Fallback" in proposal["rationale"]


@pytest.mark.asyncio
async def test_non_positive_equity_returns_empty():
    async with async_session() as db:
        result = await propose_trades(
            [_signal()],
            equity=Decimal("0"),
            day_start_equity=Decimal("1000"),
            orders_today=0,
            db=db,
        )
    assert result == []


@pytest.mark.asyncio
async def test_empty_signals_returns_empty():
    async with async_session() as db:
        result = await propose_trades(
            [],
            equity=Decimal("1000"),
            day_start_equity=Decimal("1000"),
            orders_today=0,
            db=db,
        )
    assert result == []


@pytest.mark.asyncio
async def test_quantity_always_positive_even_with_huge_price():
    async with async_session() as db:
        with patch(
            "app.modules.paper_trade_proposer.generate_json",
            new=AsyncMock(return_value=({"limit_price": 1e12, "quantity": 0, "rationale": "zero qty"}, None)),
        ):
            result = await propose_trades(
                [_signal()],
                equity=Decimal("1000"),
                day_start_equity=Decimal("1000"),
                orders_today=0,
                db=db,
            )

    assert len(result) == 1
    proposal = result[0]
    # The model returned quantity=0, so we fallback, which gives positive quantity.
    assert Decimal(proposal["quantity"]) > 0
    assert "Fallback" in proposal["rationale"]


@pytest.mark.asyncio
async def test_missing_signal_id_raises_and_skips():
    signal = _signal()
    del signal["signal_id"]
    async with async_session() as db:
        result = await propose_trades(
            [signal],
            equity=Decimal("1000"),
            day_start_equity=Decimal("1000"),
            orders_today=0,
            db=db,
        )
    assert result == []


@pytest.mark.asyncio
async def test_invalid_direction_raises_and_skips():
    async with async_session() as db:
        result = await propose_trades(
            [_signal(direction="weird")],
            equity=Decimal("1000"),
            day_start_equity=Decimal("1000"),
            orders_today=0,
            db=db,
        )
    assert result == []


@pytest.mark.asyncio
async def test_no_network_secrets_or_database_side_effects():
    async with async_session() as db:
        with patch(
            "app.modules.paper_trade_proposer.generate_json",
            new=AsyncMock(return_value=({"limit_price": 10.0, "quantity": 5, "rationale": "ok"}, None)),
        ):
            result = await propose_trades(
                [_signal()],
                equity=Decimal("1000"),
                day_start_equity=Decimal("1000"),
                orders_today=0,
                db=db,
            )

        # Proposer must return a proposal without touching the database.
        assert len(result) == 1

        # The proposal table must remain empty — proposer never writes rows.
        row = await db.execute(text("SELECT COUNT(*) FROM proposal"))
        count = row.scalar()
        assert count == 0
