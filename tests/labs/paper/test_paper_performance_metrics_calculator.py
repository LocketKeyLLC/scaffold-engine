# tests/test_paper_performance_metrics_calculator.py
"""Tests for the paper performance metrics calculator module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_performance_metrics_calculator import (
    _get_performance_metrics_with_db,
    _to_decimal,
    get_performance_metrics,
)


def _make_pnl_params(
    as_of_ts: datetime,
    account_value: str = "100000.00",
    cash: str = "50000.00",
    equity: str = "100000.00",
    realized_pnl: str = "0.00",
    unrealized_pnl: str = "0.00",
    daily_return: str | None = None,
    cumulative_return: str | None = None,
    max_drawdown: str | None = None,
    sharpe_ratio: str | None = None,
    turnover: str | None = None,
    spy_benchmark_return: str | None = None,
) -> dict:
    return {
        "as_of_ts": as_of_ts,
        "account_value": Decimal(account_value),
        "cash": Decimal(cash),
        "equity": Decimal(equity),
        "realized_pnl": Decimal(realized_pnl),
        "unrealized_pnl": Decimal(unrealized_pnl),
        "daily_return": Decimal(daily_return) if daily_return is not None else None,
        "cumulative_return": Decimal(cumulative_return) if cumulative_return is not None else None,
        "max_drawdown": Decimal(max_drawdown) if max_drawdown is not None else None,
        "sharpe_ratio": Decimal(sharpe_ratio) if sharpe_ratio is not None else None,
        "turnover": Decimal(turnover) if turnover is not None else None,
        "spy_benchmark_return": Decimal(spy_benchmark_return) if spy_benchmark_return is not None else None,
    }


async def _insert_pnl(db, *, as_of_ts: datetime, **kwargs) -> None:
    params = {
        "as_of_ts": as_of_ts,
        "account_value": Decimal(kwargs.get("account_value", "100000.00")),
        "cash": Decimal(kwargs.get("cash", "50000.00")),
        "equity": Decimal(kwargs.get("equity", "100000.00")),
        "realized_pnl": Decimal(kwargs.get("realized_pnl", "0.00")),
        "unrealized_pnl": Decimal(kwargs.get("unrealized_pnl", "0.00")),
        "daily_return": Decimal(kwargs["daily_return"]) if "daily_return" in kwargs else None,
        "cumulative_return": Decimal(kwargs["cumulative_return"]) if "cumulative_return" in kwargs else None,
        "max_drawdown": Decimal(kwargs["max_drawdown"]) if "max_drawdown" in kwargs else None,
        "sharpe_ratio": Decimal(kwargs["sharpe_ratio"]) if "sharpe_ratio" in kwargs else None,
        "turnover": Decimal(kwargs["turnover"]) if "turnover" in kwargs else None,
        "spy_benchmark_return": Decimal(kwargs["spy_benchmark_return"]) if "spy_benchmark_return" in kwargs else None,
    }
    await db.execute(
        text(
            """
            INSERT INTO pnl
                (as_of_ts, account_value, cash, equity, realized_pnl,
                 unrealized_pnl, daily_return, cumulative_return,
                 max_drawdown, sharpe_ratio, turnover, spy_benchmark_return)
            VALUES
                (:as_of_ts, :account_value, :cash, :equity, :realized_pnl,
                 :unrealized_pnl, :daily_return, :cumulative_return,
                 :max_drawdown, :sharpe_ratio, :turnover, :spy_benchmark_return)
            """
        ),
        params,
    )
    await db.commit()


async def _insert_fill(
    db,
    *,
    commission: str = "0.00",
    quantity: str = "10",
    price: str = "100.00",
    published_ts: datetime | None = None,
) -> None:
    if published_ts is None:
        published_ts = datetime.now(timezone.utc)
    external_id = f"ext-{uuid4().hex}"

    ingested_result = await db.execute(
        text(
            """
            INSERT INTO ingested_item (source, external_id, published_ts, payload)
            VALUES ('news', :external_id, :published_ts, CAST(:payload AS json))
            RETURNING id
            """
        ),
        {
            "external_id": external_id,
            "published_ts": published_ts,
            "payload": '{"headline": "test"}',
        },
    )
    ingested_id = ingested_result.scalar_one()

    signal_result = await db.execute(
        text(
            """
            INSERT INTO signal
                (ingested_item_id, ticker, signal_type, direction, rationale)
            VALUES
                (:ingested_item_id, 'AAPL', 'news_context', 'long', 'test')
            RETURNING id
            """
        ),
        {"ingested_item_id": ingested_id},
    )
    signal_id = signal_result.scalar_one()

    proposal_result = await db.execute(
        text(
            """
            INSERT INTO proposal
                (signal_id, ticker, side, quantity, order_type, rationale)
            VALUES
                (:signal_id, 'AAPL', 'buy', :quantity, 'market', 'test')
            RETURNING id
            """
        ),
        {"signal_id": signal_id, "quantity": quantity},
    )
    proposal_id = proposal_result.scalar_one()

    approval_result = await db.execute(
        text(
            """
            INSERT INTO approval
                (proposal_id, operator_id, decision)
            VALUES
                (:proposal_id, 'operator-test', 'approved')
            RETURNING id
            """
        ),
        {"proposal_id": proposal_id},
    )
    approval_id = approval_result.scalar_one()

    broker_order_id_str = f"brk-{uuid4().hex}"
    broker_order_result = await db.execute(
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
            "broker_order_id": broker_order_id_str,
        },
    )
    broker_order_row_id = broker_order_result.scalar_one()

    await db.execute(
        text(
            """
            INSERT INTO fill
                (broker_order_id, fill_id, side, quantity, price,
                 commission, filled_ts)
            VALUES
                (:broker_order_id, :fill_id, 'buy', :quantity, :price,
                 :commission, :filled_ts)
            """
        ),
        {
            "broker_order_id": broker_order_row_id,
            "fill_id": f"fill-{uuid4().hex}",
            "quantity": quantity,
            "price": price,
            "commission": commission,
            "filled_ts": published_ts,
        },
    )
    await db.commit()


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_empty():
    async with async_session() as db:
        with patch(
            "app.modules.paper_alpaca_paper_client.values_for",
            new=AsyncMock(return_value={
                "ALPACA_API_KEY_ID": "test-key-id",
                "ALPACA_API_SECRET_KEY": "test-secret-key",
            }),
        ), patch(
            "app.modules.paper_spy_benchmark_tracker.AlpacaPaperClient",
        ):
            metrics = await _get_performance_metrics_with_db(db)

    assert metrics["account_value"] == Decimal("0")
    assert metrics["cash"] == Decimal("0")
    assert metrics["equity"] == Decimal("0")
    assert metrics["return_after_costs"] == Decimal("0")
    assert metrics["max_drawdown"] == Decimal("0")
    assert metrics["sharpe_ratio"] == Decimal("0")
    assert metrics["turnover"] == Decimal("0")
    assert metrics["spy_benchmark_return"] == Decimal("0")


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_basic():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts, account_value="100000.00", equity="100000.00", daily_return="0.001")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=1), account_value="101000.00", equity="101000.00", daily_return="0.01")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=2), account_value="99000.00", equity="99000.00", daily_return="-0.02")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=3), account_value="103000.00", equity="103000.00", daily_return="0.04")
        await _insert_fill(db, commission="5.00", quantity="10", price="100.00")
        await _insert_fill(db, commission="3.00", quantity="5", price="200.00")

        with patch(
            "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
            new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0.05")}),
        ):
            metrics = await _get_performance_metrics_with_db(db)

    assert metrics["account_value"] == Decimal("103000.00")
    assert metrics["cash"] == Decimal("50000.00")
    assert metrics["equity"] == Decimal("103000.00")
    assert metrics["cumulative_return"] == Decimal("0.03")
    assert metrics["return_after_costs"] == Decimal("0.03") - Decimal("0.00008")
    expected_turnover = Decimal("2000") / Decimal("100750")
    assert metrics["turnover"] == expected_turnover
    assert metrics["spy_benchmark_return"] == Decimal("0.05")


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_single_row():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts, account_value="100000.00", equity="100000.00")

        with patch(
            "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
            new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0")}),
        ):
            metrics = await _get_performance_metrics_with_db(db)

    assert metrics["account_value"] == Decimal("100000.00")
    assert metrics["cumulative_return"] == Decimal("0")
    assert metrics["return_after_costs"] == Decimal("0")
    assert metrics["turnover"] == Decimal("0")
    assert metrics["max_drawdown"] == Decimal("0")
    assert metrics["sharpe_ratio"] == Decimal("0")


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_zero_initial_account():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts, account_value="0", equity="0")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=1), account_value="1000", equity="1000")
        await _insert_fill(db, commission="1.00", quantity="1", price="100.00")

        with patch(
            "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
            new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0")}),
        ):
            metrics = await _get_performance_metrics_with_db(db)

    assert metrics["cumulative_return"] == Decimal("0")
    assert metrics["return_after_costs"] == Decimal("0")
    assert metrics["turnover"] == Decimal("100") / Decimal("500")


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_max_drawdown():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts, equity="100000.00")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=1), equity="110000.00")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=2), equity="99000.00")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=3), equity="105000.00")

        with patch(
            "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
            new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0")}),
        ):
            metrics = await _get_performance_metrics_with_db(db)

    assert metrics["max_drawdown"] == Decimal("-0.1")


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_sharpe_ratio():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts, daily_return="0.01")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=1), daily_return="0.02")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=2), daily_return="0.03")

        with patch(
            "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
            new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0")}),
        ):
            metrics = await _get_performance_metrics_with_db(db)

    expected = (Decimal("0.02") / Decimal("0.01")) * Decimal("252").sqrt()
    assert metrics["sharpe_ratio"] == expected


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_sharpe_ratio_insufficient_data():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts, daily_return="0.01")

        with patch(
            "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
            new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0")}),
        ):
            metrics = await _get_performance_metrics_with_db(db)

    assert metrics["sharpe_ratio"] == Decimal("0")


@pytest.mark.asyncio
async def test_get_performance_metrics_with_db_spy_benchmark_called():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts)
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=5))

        mock_spy = AsyncMock(return_value={"buy_and_hold_return": Decimal("0.07")})
        with patch(
            "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
            new=mock_spy,
        ):
            metrics = await _get_performance_metrics_with_db(db)

    mock_spy.assert_awaited_once()
    assert metrics["spy_benchmark_return"] == Decimal("0.07")


@pytest.mark.asyncio
async def test_get_performance_metrics_persists_via_write_pnl():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts, account_value="100000.00", equity="100000.00")
        await _insert_pnl(db, as_of_ts=base_ts + timedelta(days=1), account_value="101000.00", equity="101000.00")
        await _insert_fill(db, commission="2.00", quantity="10", price="100.00")

    with patch(
        "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
        new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0")}),
    ):
        await get_performance_metrics()

    async with async_session() as db:
        count_result = await db.execute(text("SELECT COUNT(*) FROM pnl"))
        count = count_result.scalar_one()
        assert count == 3  # two inserted + one persisted

        latest_result = await db.execute(
            text("SELECT account_value, cash, equity FROM pnl ORDER BY id DESC LIMIT 1")
        )
        latest = latest_result.mappings().one()
        assert Decimal(latest["account_value"]) == Decimal("101000.00")
        assert Decimal(latest["cash"]) == Decimal("50000.00")
        assert Decimal(latest["equity"]) == Decimal("101000.00")


@pytest.mark.asyncio
async def test_get_performance_metrics_write_pnl_keyword_only():
    base_ts = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    async with async_session() as db:
        await _insert_pnl(db, as_of_ts=base_ts)

    with patch(
        "app.modules.paper_performance_metrics_calculator.get_spy_benchmark",
        new=AsyncMock(return_value={"buy_and_hold_return": Decimal("0")}),
    ):
        await get_performance_metrics()

    async with async_session() as db:
        count_result = await db.execute(text("SELECT COUNT(*) FROM pnl"))
        count = count_result.scalar_one()
        assert count == 2  # one inserted + one persisted by write_pnl

        latest_result = await db.execute(
            text("SELECT account_value, cash, equity FROM pnl ORDER BY id DESC LIMIT 1")
        )
        latest = latest_result.mappings().one()
        assert Decimal(latest["account_value"]) == Decimal("100000.00")
        assert Decimal(latest["cash"]) == Decimal("50000.00")
        assert Decimal(latest["equity"]) == Decimal("100000.00")


def test_to_decimal_none():
    assert _to_decimal(None) == Decimal("0")


def test_to_decimal_decimal():
    d = Decimal("1.5")
    assert _to_decimal(d) == d
    assert _to_decimal(d) is d


def test_to_decimal_str():
    assert _to_decimal("3.14") == Decimal("3.14")


def test_to_decimal_int():
    assert _to_decimal(42) == Decimal("42")


def test_to_decimal_float():
    assert _to_decimal(0.5) == Decimal("0.5")
