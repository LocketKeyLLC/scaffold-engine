# tests/test_paper_weekly_strategy_re_evaluation.py
"""Tests for the weekly strategy re-evaluation module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.paper_weekly_strategy_re_evaluation import (
    DEFAULT_LOOKBACK_YEARS,
    PAUSE_THRESHOLD,
    re_evaluate_strategy,
)


@pytest.fixture
def mock_backtest():
    with patch(
        "app.modules.paper_weekly_strategy_re_evaluation.run_backtest",
        new_callable=AsyncMock,
    ) as m:
        yield m


@pytest.mark.asyncio
async def test_profitable_backtest_returns_continue(mock_backtest):
    mock_backtest.return_value = {"total_return": 0.05}
    result = await re_evaluate_strategy()

    assert result["decision"] == "continue"
    assert result["paused"] is False
    assert result["total_return"] == pytest.approx(0.05)
    assert "may continue" in result["reason"]


@pytest.mark.asyncio
async def test_losing_backtest_returns_pause(mock_backtest):
    mock_backtest.return_value = {"total_return": -0.03}
    result = await re_evaluate_strategy()

    assert result["decision"] == "pause"
    assert result["paused"] is True
    assert result["total_return"] == pytest.approx(-0.03)
    assert "operator approval required" in result["reason"]


@pytest.mark.asyncio
async def test_break_even_backtest_returns_pause(mock_backtest):
    mock_backtest.return_value = {"total_return": 0.0}
    result = await re_evaluate_strategy()

    assert result["decision"] == "pause"
    assert result["paused"] is True
    assert result["total_return"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_uses_equity_curve_when_total_return_missing(mock_backtest):
    mock_backtest.return_value = {
        "equity_curve": [Decimal("100000"), Decimal("98000")],
    }
    result = await re_evaluate_strategy()

    assert result["decision"] == "pause"
    assert result["total_return"] == pytest.approx(-0.02)


@pytest.mark.asyncio
async def test_single_point_equity_curve_is_flat(mock_backtest):
    mock_backtest.return_value = {"equity_curve": [Decimal("100000")]}
    result = await re_evaluate_strategy()

    assert result["decision"] == "pause"
    assert result["total_return"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_missing_metrics_returns_pause(mock_backtest):
    mock_backtest.return_value = {}
    result = await re_evaluate_strategy()

    assert result["decision"] == "pause"
    assert result["paused"] is True
    assert result["total_return"] is None
    assert "missing return metrics" in result["reason"]


@pytest.mark.asyncio
async def test_backtest_exception_returns_pause(mock_backtest):
    mock_backtest.side_effect = RuntimeError("network unreachable")
    result = await re_evaluate_strategy()

    assert result["decision"] == "pause"
    assert result["paused"] is True
    assert result["total_return"] is None
    assert "network unreachable" in result["reason"]
    assert "operator approval required" in result["reason"]


@pytest.mark.asyncio
async def test_custom_lookback_years(mock_backtest):
    mock_backtest.return_value = {"total_return": 0.01}
    result = await re_evaluate_strategy(lookback_years=1)

    assert result["lookback_years"] == 1
    start = result["start_date"]
    end = result["end_date"]
    delta = end - start
    assert abs(delta.days - 365) <= 1


@pytest.mark.asyncio
async def test_default_lookback_is_two_years(mock_backtest):
    mock_backtest.return_value = {"total_return": 0.01}
    result = await re_evaluate_strategy()

    assert result["lookback_years"] == DEFAULT_LOOKBACK_YEARS
    delta = result["end_date"] - result["start_date"]
    assert abs(delta.days - 365 * DEFAULT_LOOKBACK_YEARS) <= 1


@pytest.mark.asyncio
async def test_invalid_lookback_raises(mock_backtest):
    with pytest.raises(ValueError, match="lookback_years must be at least 1"):
        await re_evaluate_strategy(lookback_years=0)


@pytest.mark.asyncio
async def test_passes_db_to_backtester(mock_backtest):
    fake_db = object()
    mock_backtest.return_value = {"total_return": 0.01}
    await re_evaluate_strategy(db=fake_db)

    mock_backtest.assert_awaited_once()
    _, kwargs = mock_backtest.await_args
    assert kwargs.get("db") is fake_db


@pytest.mark.asyncio
async def test_no_network_database_or_model_calls(mock_backtest):
    mock_backtest.return_value = {"total_return": 0.01}
    with patch("app.database.async_session") as mock_session, patch(
        "app.utils.http_clients.get_generic_http_client"
    ) as mock_http, patch(
        "app.model_router.generate"
    ) as mock_generate:
        await re_evaluate_strategy()

    mock_session.assert_not_called()
    mock_http.assert_not_called()
    mock_generate.assert_not_called()
