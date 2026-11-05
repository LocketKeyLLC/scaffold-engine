# tests/test_paper_walk_forward_backtester.py
"""Tests for the walk-forward backtester."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_signal
from app.modules.paper_walk_forward_backtester import (
    FEE_RATE,
    INITIAL_EQUITY,
    MAX_PROPOSALS_PER_BATCH,
    SLIPPAGE_RATE,
    TEST_WINDOW_DAYS,
    TRAIN_WINDOW_DAYS,
    _compute_benchmark_curve,
    _compute_max_drawdown,
    _compute_sharpe_ratio,
    _compute_total_return,
    _compute_turnover,
    _default_model_trainer,
    _filter_by_publication_time,
    _mark_to_market,
    run_backtest,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Truncate all audit trail tables before each test."""
    async with async_session() as db:
        for table in (
            "pnl",
            "fill",
            "broker_order_id",
            "approval",
            "risk_check",
            "proposal",
            "signal",
            "ingested_item",
        ):
            await db.execute(text(f"TRUNCATE {table} CASCADE"))
        await db.commit()
    yield


class FakeAlpacaClient:
    """Fake Alpaca client for tests that need one."""

    def __init__(self):
        self.orders: list = []
        self.positions: dict = {}
        self.submitted: list = []

    async def get_account(self):
        return None

    async def get_clock(self):
        return None

    async def submit_order(self, *, symbol, qty, side, order_type, **kwargs):
        self.submitted.append(
            {
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "order_type": order_type,
                **kwargs,
            }
        )
        return {"id": uuid4().hex, "symbol": symbol}

    async def get_bars(self, symbol, *, timeframe="1Day", start=None, end=None, limit=None):
        return []

    async def list_positions(self):
        return []

    async def close(self):
        pass


def _spy_metrics() -> dict:
    """Return a valid get_spy_benchmark result dict."""
    return {
        "start_date": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "end_date": datetime(2026, 3, 1, tzinfo=timezone.utc),
        "buy_and_hold_return": 0.05,
        "max_drawdown": -0.03,
        "sharpe_ratio": 0.8,
        "turnover": 0.0,
        "num_prices": 60,
    }


@pytest.fixture
def patch_external():
    """Patch external calls: propose_trades (model) and get_spy_benchmark (network)."""
    with patch(
        "app.modules.paper_walk_forward_backtester.propose_trades",
        new_callable=AsyncMock,
    ) as mock_propose, patch(
        "app.modules.paper_walk_forward_backtester.get_spy_benchmark",
        new_callable=AsyncMock,
    ) as mock_spy:
        mock_propose.return_value = []
        mock_spy.return_value = _spy_metrics()
        yield mock_propose, mock_spy


async def _insert_ingested_item(
    db,
    *,
    source: str = "sec_edgar",
    published_ts: datetime | None = None,
    payload: dict | None = None,
) -> int:
    """Insert an ingested_item row and return its id."""
    if published_ts is None:
        published_ts = datetime(2026, 1, 15, tzinfo=timezone.utc)
    if payload is None:
        payload = {
            "ticker": "AAPL",
            "score": 0.7,
            "publication_time": published_ts.isoformat(),
        }
    result = await db.execute(
        text(
            """
            INSERT INTO ingested_item (source, external_id, published_ts, payload)
            VALUES (:source, :external_id, :published_ts, CAST(:payload AS json))
            RETURNING id
            """
        ),
        {
            "source": source,
            "external_id": uuid4().hex,
            "published_ts": published_ts,
            "payload": json.dumps(payload),
        },
    )
    await db.commit()
    return result.scalar_one()


async def _insert_signal(
    db,
    *,
    ingested_item_id: int,
    ticker: str = "AAPL",
    signal_type: str = "form4_purchase",
    direction: str = "long",
) -> int:
    """Insert a signal row via write_signal and return its id."""
    return await write_signal(
        ingested_item_id=ingested_item_id,
        ticker=ticker,
        signal_type=signal_type,
        direction=direction,
        rationale="test signal",
    )


def _proposal(
    ticker: str = "AAPL",
    side: str = "buy",
    quantity: str = "10",
    limit_price: str = "150.0",
) -> dict:
    """Build a proposal dict with the keys the backtester reads."""
    return {
        "ticker": ticker,
        "side": side,
        "quantity": Decimal(quantity),
        "limit_price": Decimal(limit_price),
    }


# --- _mark_to_market ---


def test_mark_to_market_returns_cash_when_no_holdings():
    result = _mark_to_market({}, {}, Decimal("1000"))
    assert result == Decimal("1000")


def test_mark_to_market_uses_prices():
    holdings = {"AAPL": Decimal("10")}
    prices = {"AAPL": Decimal("150")}
    result = _mark_to_market(holdings, prices, Decimal("1000"))
    assert result == Decimal("2500")


def test_mark_to_market_ignores_missing_price():
    holdings = {"AAPL": Decimal("10")}
    result = _mark_to_market(holdings, {}, Decimal("1000"))
    assert result == Decimal("1000")


def test_mark_to_market():
    holdings = {"AAPL": Decimal("10"), "MSFT": Decimal("5")}
    prices = {"AAPL": Decimal("100"), "MSFT": Decimal("200")}
    cash = Decimal("1000")
    assert _mark_to_market(holdings, prices, cash) == Decimal("3000")

    holdings = {"AAPL": Decimal("10"), "TSLA": Decimal("1")}
    prices = {"AAPL": Decimal("100")}
    assert _mark_to_market(holdings, prices, Decimal("0")) == Decimal("1000")


# --- _compute_turnover ---


def test_compute_turnover():
    assert _compute_turnover([Decimal("100"), Decimal("-50")], Decimal("1000")) == pytest.approx(0.15)
    assert _compute_turnover([], Decimal("1000")) == 0.0
    assert _compute_turnover([Decimal("100")], Decimal("0")) == 0.0
    assert _compute_turnover([Decimal("100")], Decimal("-5")) == 0.0


def test_compute_turnover_zero_equity():
    result = _compute_turnover([Decimal("100")], Decimal("0"))
    assert result == 0.0


# --- _compute_max_drawdown ---


def test_compute_max_drawdown():
    assert _compute_max_drawdown([]) == 0.0
    assert _compute_max_drawdown([Decimal("100"), Decimal("110"), Decimal("99")]) == pytest.approx(-0.1)
    assert _compute_max_drawdown([Decimal("100"), Decimal("90"), Decimal("95")]) == pytest.approx(-0.1)
    assert _compute_max_drawdown([Decimal("100"), Decimal("110")]) == 0.0


def test_compute_max_drawdown_no_drawdown():
    curve = [Decimal("100"), Decimal("110"), Decimal("120")]
    result = _compute_max_drawdown(curve)
    assert result == 0.0


def test_compute_max_drawdown_empty():
    result = _compute_max_drawdown([])
    assert result == 0.0


# --- _compute_sharpe_ratio ---


def test_compute_sharpe_ratio():
    assert _compute_sharpe_ratio([]) == 0.0
    assert _compute_sharpe_ratio([Decimal("100")]) == 0.0
    assert _compute_sharpe_ratio([Decimal("100"), Decimal("100")]) == 0.0
    curve = [Decimal("100"), Decimal("110"), Decimal("105")]
    assert _compute_sharpe_ratio(curve) > 0


def test_compute_sharpe_ratio_constant():
    curve = [Decimal("100"), Decimal("100"), Decimal("100")]
    result = _compute_sharpe_ratio(curve)
    assert result == 0.0


def test_compute_sharpe_ratio_single_point():
    result = _compute_sharpe_ratio([Decimal("100")])
    assert result == 0.0


def test_compute_sharpe_ratio_empty():
    result = _compute_sharpe_ratio([])
    assert result == 0.0


# --- _compute_total_return ---


def test_compute_total_return():
    assert _compute_total_return([Decimal("100"), Decimal("110")]) == pytest.approx(0.1)
    assert _compute_total_return([]) == 0.0
    assert _compute_total_return([Decimal("0"), Decimal("10")]) == 0.0


def test_compute_total_return_zero_start():
    result = _compute_total_return([Decimal("0"), Decimal("10")])
    assert result == 0.0


def test_compute_total_return_empty():
    result = _compute_total_return([])
    assert result == 0.0


# --- _default_model_trainer ---


def test_default_model_trainer():
    trainer = _default_model_trainer([])
    assert trainer({"payload": {"score": 0.9}}) is True
    assert trainer({"payload": {"score": 0.4}}) is False
    assert trainer({"payload": {}}) is False
    assert trainer({}) is False
    assert trainer({"payload": {"score": "not-a-number"}}) is False
    mock_item = MagicMock()
    mock_item.get.return_value = {"score": 0.9}
    assert trainer(mock_item) is True


# --- _filter_by_publication_time ---


def test_filter_by_publication_time():
    as_of = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [
        {"publication_time": datetime(2026, 1, 5, tzinfo=timezone.utc), "id": 1},
        {"publication_time": datetime(2026, 1, 15, tzinfo=timezone.utc), "id": 2},
        {"publication_time": "2026-01-08T00:00:00Z", "id": 3},
        {"id": 4},
        {"publication_time": "not-a-date", "id": 5},
    ]
    result = _filter_by_publication_time(items, as_of)
    assert [i["id"] for i in result] == [1, 3]


# --- _compute_benchmark_curve ---


def test_compute_benchmark_curve():
    dates = [datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i) for i in range(3)]
    metrics = {"buy_and_hold_return": 0.1}
    curve = _compute_benchmark_curve(metrics, dates)
    assert len(curve) == 3
    assert curve[0] == Decimal("1.0")
    assert curve[-1] == pytest.approx(Decimal("1.1"))
    assert _compute_benchmark_curve(metrics, []) == []
    single = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    assert _compute_benchmark_curve(metrics, single) == [Decimal("1.0")]


def test_compute_benchmark_curve_empty_dates():
    result = _compute_benchmark_curve(_spy_metrics(), [])
    assert result == []


# --- run_backtest ---


@pytest.mark.asyncio
async def test_invalid_date_range_raises(patch_external):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2023, 12, 31, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="end_date must be after start_date"):
        await run_backtest(start, end)


@pytest.mark.asyncio
async def test_invalid_date_range_raises_async():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start
    async with async_session() as db:
        with pytest.raises(ValueError, match="end_date must be after start_date"):
            await run_backtest(start, end, db=db)


@pytest.mark.asyncio
async def test_run_backtest_empty_data(patch_external):
    propose_mock, spy_mock = patch_external
    propose_mock.return_value = []
    spy_mock.return_value = _spy_metrics()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 10, tzinfo=timezone.utc)
    async with async_session() as db:
        result = await run_backtest(start, end, db=db)

    assert result["trade_count"] == 0
    assert result["total_return"] == 0.0
    assert result["equity_curve"] == [float(INITIAL_EQUITY)]
    assert result["spy_total_return"] == 0.05


async def test_run_backtest_handles_unequal_windows_without_strict_zip_error(patch_external):
    """A short date range yields unequal train/test window counts; the backtest
    must not raise a strict-zip ValueError."""
    mock_propose, mock_spy = patch_external
    mock_propose.return_value = []
    mock_spy.return_value = _spy_metrics()

    async with async_session() as db:
        result = await run_backtest(
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 10, tzinfo=timezone.utc),
            db=db,
        )

    assert result is not None
    assert "total_return" in result


@pytest.mark.asyncio
async def test_run_backtest_window_splitting(patch_external):
    propose_mock, spy_mock = patch_external
    propose_mock.return_value = [
        {"ticker": "AAPL", "side": "buy", "quantity": "1", "limit_price": "100"},
    ]
    spy_mock.return_value = _spy_metrics()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=TRAIN_WINDOW_DAYS * 2 + TEST_WINDOW_DAYS * 2)
    async with async_session() as db:
        result = await run_backtest(start, end, db=db)

    assert propose_mock.await_count == 2
    assert result["trade_count"] == 2


@pytest.mark.asyncio
async def test_run_backtest_no_test_windows_fallback(patch_external):
    propose_mock, spy_mock = patch_external
    propose_mock.return_value = [
        {"ticker": "AAPL", "side": "buy", "quantity": "1", "limit_price": "100"},
    ]
    spy_mock.return_value = _spy_metrics()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=5)
    async with async_session() as db:
        result = await run_backtest(start, end, db=db)

    assert propose_mock.await_count == 1
    assert result["trade_count"] == 1


@pytest.mark.asyncio
async def test_run_backtest_respects_max_proposals_per_batch(patch_external):
    propose_mock, spy_mock = patch_external
    proposals = [
        {"ticker": f"T{i}", "side": "buy", "quantity": "1", "limit_price": "10"}
        for i in range(MAX_PROPOSALS_PER_BATCH + 2)
    ]
    propose_mock.return_value = proposals
    spy_mock.return_value = _spy_metrics()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 10, tzinfo=timezone.utc)
    async with async_session() as db:
        result = await run_backtest(start, end, db=db)

    assert result["trade_count"] == MAX_PROPOSALS_PER_BATCH


async def test_run_backtest_uses_real_get_data_as_of(patch_external):
    """get_data_as_of is the real paper_point_time_store function; it reads
    ingested_item rows. Insert those rows and let the backtest read them."""
    mock_propose, mock_spy = patch_external
    mock_propose.return_value = []
    mock_spy.return_value = _spy_metrics()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=TRAIN_WINDOW_DAYS + TEST_WINDOW_DAYS)

    async with async_session() as db:
        # Insert an ingested_item row the backtest will read via get_data_as_of.
        await _insert_ingested_item(
            db,
            source="sec_edgar",
            published_ts=start + timedelta(days=5),
            payload={
                "ticker": "AAPL",
                "score": 0.9,
                "publication_time": (start + timedelta(days=5)).isoformat(),
            },
        )

        result = await run_backtest(start, end, db=db)

    assert result is not None
    assert "equity_curve" in result


async def test_run_backtest_uses_real_generate_signals(patch_external):
    """generate_signals is the real paper_signal_generator function; it reads
    ingested_item and writes signal rows. Insert ingested_item rows first."""
    mock_propose, mock_spy = patch_external
    mock_propose.return_value = []
    mock_spy.return_value = _spy_metrics()

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=TRAIN_WINDOW_DAYS + TEST_WINDOW_DAYS)

    async with async_session() as db:
        # Insert ingested_item rows that generate_signals will normalize.
        await _insert_ingested_item(
            db,
            source="sec_edgar",
            published_ts=start + timedelta(days=10),
            payload={
                "ticker": "AAPL",
                "symbol": "AAPL",
                "score": 0.9,
                "publication_time": (start + timedelta(days=10)).isoformat(),
            },
        )
        await _insert_ingested_item(
            db,
            source="congress",
            published_ts=start + timedelta(days=20),
            payload={
                "ticker": "MSFT",
                "asset_ticker": "MSFT",
                "score": 0.6,
                "publication_time": (start + timedelta(days=20)).isoformat(),
            },
        )

        result = await run_backtest(start, end, db=db)

    assert result is not None
    assert "equity_curve" in result
