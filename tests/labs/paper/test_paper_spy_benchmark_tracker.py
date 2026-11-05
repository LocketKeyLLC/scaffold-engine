# tests/test_paper_spy_benchmark_tracker.py
"""Tests for the SPY benchmark tracker.

All network calls are mocked. Helper functions are exercised with Decimal
closes (as returned by NUMERIC columns) to ensure no Decimal/float mixing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.paper_spy_benchmark_tracker import (
    _buy_and_hold_return,
    _max_drawdown,
    _sharpe_ratio,
    _turnover,
    get_spy_benchmark,
)


def _price(close):
    """Build a price point; only 'close' matters for helper functions."""
    return {"close": close}


def test_buy_and_hold_return_basic_decimal():
    prices = [_price(Decimal("100.00")), _price(Decimal("110.00"))]
    result = _buy_and_hold_return(prices)
    expected = (110.0 / 100.0) - 1.0 - (2 * 0.0001)
    assert abs(result - expected) < 1e-9


def test_buy_and_hold_return_basic_float():
    prices = [_price(100.0), _price(110.0)]
    result = _buy_and_hold_return(prices)
    expected = 0.10 - (2 * 0.0001)
    assert abs(result - expected) < 1e-9


def test_buy_and_hold_return_empty():
    assert _buy_and_hold_return([]) == 0.0


def test_buy_and_hold_return_zero_price_raises():
    prices = [_price(Decimal("0.00"))]
    with pytest.raises(ValueError, match="SPY price must be positive"):
        _buy_and_hold_return(prices)


def test_buy_and_hold_return_negative_price_raises():
    prices = [_price(Decimal("-1.00")), _price(Decimal("1.00"))]
    with pytest.raises(ValueError, match="SPY price must be positive"):
        _buy_and_hold_return(prices)


def test_max_drawdown_basic_decimal():
    prices = [
        _price(Decimal("100.00")),
        _price(Decimal("110.00")),
        _price(Decimal("90.00")),
        _price(Decimal("95.00")),
    ]
    result = _max_drawdown(prices)
    expected = (90.0 - 110.0) / 110.0
    assert abs(result - expected) < 1e-9


def test_max_drawdown_empty():
    assert _max_drawdown([]) == 0.0


def test_max_drawdown_no_drawdown():
    prices = [_price(Decimal("100.00")), _price(Decimal("110.00"))]
    assert _max_drawdown(prices) == 0.0


def test_sharpe_ratio_constant_prices_decimal():
    prices = [
        _price(Decimal("100.00")),
        _price(Decimal("100.00")),
        _price(Decimal("100.00")),
    ]
    assert _sharpe_ratio(prices) == 0.0


def test_sharpe_ratio_constant_prices_float():
    prices = [_price(100.0), _price(100.0), _price(100.0)]
    assert _sharpe_ratio(prices) == 0.0


def test_sharpe_ratio_single_price():
    assert _sharpe_ratio([_price(Decimal("100.00"))]) == 0.0


def test_sharpe_ratio_empty():
    assert _sharpe_ratio([]) == 0.0


def test_sharpe_ratio_nonzero_returns():
    prices = [
        _price(Decimal("100.00")),
        _price(Decimal("101.00")),
        _price(Decimal("102.00")),
    ]
    result = _sharpe_ratio(prices)
    # Two daily returns: 0.01 and ~0.00990099, mean ~0.0099505, population std small
    assert result > 0.0


def test_sharpe_ratio_skips_non_positive_previous():
    prices = [
        _price(Decimal("0.00")),
        _price(Decimal("100.00")),
        _price(Decimal("101.00")),
    ]
    result = _sharpe_ratio(prices)
    assert result == 0.0  # only one valid return, std dev zero


def test_turnover():
    assert _turnover([]) == 0.0
    assert _turnover([_price(Decimal("100.00"))]) == 0.0


@pytest.mark.asyncio
async def test_get_spy_benchmark_basic():
    bars = [
        {"t": "2024-01-01T05:00:00+00:00", "c": "400.00"},
        {"t": "2024-01-02T05:00:00+00:00", "c": "404.00"},
    ]
    mock_client = MagicMock()
    mock_client.get_bars = AsyncMock(return_value=bars)
    mock_client.close = AsyncMock()

    with patch("app.modules.paper_spy_benchmark_tracker.AlpacaPaperClient", return_value=mock_client):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        result = await get_spy_benchmark(start, end)

    assert result["num_prices"] == 2
    expected_return = (404.0 / 400.0) - 1.0 - (2 * 0.0001)
    assert abs(result["buy_and_hold_return"] - expected_return) < 1e-9
    assert result["max_drawdown"] == 0.0
    assert result["sharpe_ratio"] == 0.0
    assert result["turnover"] == 0.0
    assert result["start_date"] == start.isoformat()
    assert result["end_date"] == end.isoformat()
    mock_client.get_bars.assert_awaited_once_with(
        "SPY",
        timeframe="1Day",
        start=start,
        end=end,
    )
    mock_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_spy_benchmark_no_prices():
    mock_client = MagicMock()
    mock_client.get_bars = AsyncMock(return_value=[])
    mock_client.close = AsyncMock()

    with patch("app.modules.paper_spy_benchmark_tracker.AlpacaPaperClient", return_value=mock_client):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        result = await get_spy_benchmark(start, end)

    assert result["num_prices"] == 0
    assert result["buy_and_hold_return"] == 0.0
    assert result["max_drawdown"] == 0.0
    assert result["sharpe_ratio"] == 0.0
    assert result["turnover"] == 0.0
