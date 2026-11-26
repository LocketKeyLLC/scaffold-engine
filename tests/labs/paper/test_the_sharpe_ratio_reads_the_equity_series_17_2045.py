"""§17.2045 — the Sharpe ratio is computed from the lab's own equity series, and says when there is not enough of it.

Before: the ratio was read from `pnl.daily_return`, which no writer fills (the day-start row, the after-close P&L row
and the metrics row all write it None or copy the last one), so it stayed 0 forever -- and 0 also stood for "no
history", which reads as a measured, flat strategy.

Rows here are written by the lab's own writers in its own daily order: `record_day_start_equity` at 13:00 UTC, then
the after-close P&L row at 21:30 UTC (`write_pnl`, as `calculate_pnl` writes it). The equity figures are made up --
the live lab has recorded none yet (no Alpaca keys) -- and the expected ratio is computed here independently.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import pairwise
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_performance_metrics_calculator as calc
from app.modules.paper_audit_trail_writer import write_pnl
from app.modules.paper_dashboard_view_builder import build_dashboard_context
from app.modules.paper_day_start_equity_recording import record_day_start_equity
from app.modules.paper_metrics_computation_module import compute_series

DAY0 = datetime(2026, 11, 16, tzinfo=timezone.utc)   # a Monday


async def _trading_day(i: int, opening: str, closing: str) -> None:
    day = DAY0 + timedelta(days=i)
    async with async_session() as db:
        await record_day_start_equity(db=db, equity=Decimal(opening), as_of_ts=day.replace(hour=13))
        await db.commit()
    await write_pnl(as_of_ts=day.replace(hour=21, minute=30), account_value=closing, cash="0", equity=closing,
                    realized_pnl="0", unrealized_pnl="0", daily_return=None, cumulative_return=None,
                    max_drawdown=None, sharpe_ratio=None, turnover=None, spy_benchmark_return=None)


async def _metrics() -> dict:
    with patch.object(calc, "get_spy_benchmark", AsyncMock(return_value={"buy_and_hold_return": Decimal("0")})):
        async with async_session() as db:
            return await calc._get_performance_metrics_with_db(db)


@pytest.mark.asyncio
async def test_four_closes_give_three_daily_returns_and_their_sharpe() -> None:
    closes = ["100000", "101000", "99500", "102000"]
    opening = "100000"
    for i, close in enumerate(closes):
        await _trading_day(i, opening, close)
        opening = close
    m = await _metrics()
    returns = [float(b) / float(a) - 1 for a, b in pairwise(closes)]
    expected = statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(252)
    assert m["sharpe_ratio"] is not None
    assert float(m["sharpe_ratio"]) == pytest.approx(expected, rel=1e-9)
    assert float(m["daily_return"]) == pytest.approx(returns[-1], rel=1e-12)   # the latest close over the one before
    assert "sharpe_ratio_note" not in m


@pytest.mark.asyncio
async def test_fewer_than_two_returns_is_not_enough_data_not_zero() -> None:
    assert (await _metrics())["sharpe_ratio"] is None                          # no history at all
    await _trading_day(0, "100000", "100500")
    await _trading_day(1, "100500", "101000")                                  # two closes: one return
    m = await _metrics()
    assert m["sharpe_ratio"] is None
    assert m["sharpe_ratio_note"].startswith("not enough data")
    assert float(m["daily_return"]) == pytest.approx(101000 / 100500 - 1, rel=1e-12)


@pytest.mark.asyncio
async def test_the_metrics_row_stores_null_and_the_real_daily_return() -> None:
    await _trading_day(0, "100000", "100000")
    await _trading_day(1, "100000", "100250")
    with patch.object(calc, "get_spy_benchmark", AsyncMock(return_value={"buy_and_hold_return": Decimal("0")})):
        await calc.get_performance_metrics()
    async with async_session() as db:
        row = (await db.execute(text(
            "SELECT sharpe_ratio, daily_return FROM pnl ORDER BY as_of_ts DESC, id DESC LIMIT 1"))).one()
    assert row[0] is None                                                      # not 0, not the text "None"
    assert float(row[1]) == pytest.approx(0.0025, rel=1e-9)


def test_the_dashboard_says_not_enough_data_instead_of_zero() -> None:
    ts = datetime(2026, 11, 17, 21, 30)
    view = [{"as_of_ts": ts, "account_value": 100250.0, "cumulative_return": 0.0025, "max_drawdown": 0.0,
             "sharpe_ratio": None, "turnover": 0.0}]
    spy = [(ts, 600.0)]
    assert compute_series(view, spy, "all")["sharpe_ratio"] is None
    assert compute_series([], spy, "all")["sharpe_ratio"] is None
    ctx = build_dashboard_context(view, {}, [], spy, "all")
    assert ctx["sharpe"] == "not enough data"
