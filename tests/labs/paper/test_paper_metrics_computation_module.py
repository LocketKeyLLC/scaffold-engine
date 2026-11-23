# tests/test_paper_metrics_computation_module.py
import pytest
from datetime import datetime, timedelta, date
from typing import Any, Dict, Tuple

from app.modules.paper_metrics_computation_module import (
    compute_series,
    filter_by_span,
    SPAN_DAYS,
)
from app.database import async_session


def make_view_item(
    ts: datetime,
    account_value: float,
    cumulative_return: float,
    costs: float = 0.0,
    max_drawdown: float = 0.0,
    sharpe_ratio: float = 0.0,
    turnover: float = 0.0,
    gross_return: float = None,
) -> Dict[str, Any]:
    d = {
        "as_of_ts": ts,
        "account_value": account_value,
        "cumulative_return": cumulative_return,
        "costs": costs,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "turnover": turnover,
    }
    if gross_return is not None:
        d["gross_return"] = gross_return
    return d


def make_spy_item(ts: datetime, price: float) -> Tuple[datetime, float]:
    return (ts, price)


# ---------------------------------------------------------------
# filter_by_span tests
# ---------------------------------------------------------------
def test_filter_by_span_all_returns_all():
    items = [
        {"as_of_ts": datetime(2026, 1, 1)},
        {"as_of_ts": datetime(2026, 1, 2)},
        {"as_of_ts": datetime(2026, 1, 3)},
    ]
    result = filter_by_span(items, "all")
    assert result == items


def test_filter_by_span_1w_filters_correctly():
    now = datetime(2026, 1, 10, 12, 0, 0)
    items = [
        {"as_of_ts": now - timedelta(days=10)},
        {"as_of_ts": now - timedelta(days=6)},
        {"as_of_ts": now - timedelta(days=3)},
        {"as_of_ts": now},
    ]
    filtered = filter_by_span(items, "1w")
    # cutoff = now - 7 days => should include items with ts >= cutoff
    expected = [items[1], items[2], items[3]]
    assert filtered == expected


def test_filter_by_span_invalid_span_raises():
    with pytest.raises(ValueError):
        filter_by_span([], "1y")


def test_filter_by_span_empty_items_returns_empty():
    assert filter_by_span([], "all") == []


def test_filter_by_span_no_timestamps_returns_all():
    items = [{"foo": 1}, {"foo": 2}]
    assert filter_by_span(items, "all") == items


# ---------------------------------------------------------------
# compute_series tests
# ---------------------------------------------------------------
def test_compute_series_basic_alignment_and_summary():
    # Two dates common to both view and spy
    ts1 = datetime(2026, 1, 1, 16, 0, 0)
    ts2 = datetime(2026, 1, 2, 16, 0, 0)
    view = [
        make_view_item(ts1, account_value=10000.0, cumulative_return=0.01, costs=5.0,
                       max_drawdown=0.0, sharpe_ratio=1.2, turnover=0.1),
        make_view_item(ts2, account_value=10100.0, cumulative_return=0.02, costs=8.0,
                       max_drawdown=-0.01, sharpe_ratio=1.5, turnover=0.15),
    ]
    spy = [make_spy_item(ts1, 500.0), make_spy_item(ts2, 505.0)]

    result = compute_series(view, spy, "all")

    assert result["dates"] == [ts1.date().isoformat(), ts2.date().isoformat()]
    assert result["account_values"] == [10000.0, 10100.0]
    assert result["spy_values"] == [500.0, 505.0]
    assert result["net_return"] == 0.02
    assert result["gross_return"] == pytest.approx(0.02 + 8.0)
    assert result["costs"] == 8.0
    assert result["max_drawdown"] == -0.01
    assert result["sharpe_ratio"] == 1.5
    assert result["turnover"] == 0.15


def test_compute_series_uses_gross_return_if_present():
    ts = datetime(2026, 1, 1, 16, 0, 0)
    view = [make_view_item(ts, 10000.0, 0.02, costs=8.0, gross_return=0.025)]
    spy = [make_spy_item(ts, 500.0)]
    result = compute_series(view, spy, "all")
    assert result["gross_return"] == 0.025


def test_compute_series_empty_view_returns_zeros():
    result = compute_series([], [(datetime(2026,1,1), 500.0)], "all")
    assert result["dates"] == []
    assert result["account_values"] == []
    assert result["spy_values"] == []
    assert result["net_return"] == 0.0
    assert result["gross_return"] == 0.0
    assert result["costs"] == 0.0
    assert result["max_drawdown"] == 0.0
    assert result["sharpe_ratio"] == 0.0
    assert result["turnover"] == 0.0


def test_compute_series_no_common_dates_returns_zeros():
    view = [make_view_item(datetime(2026,1,1), 10000.0, 0.01)]
    spy = [make_spy_item(datetime(2026,1,2), 500.0)]
    result = compute_series(view, spy, "all")
    assert result["dates"] == []
    assert result["account_values"] == []
    assert result["spy_values"] == []


def test_compute_series_invalid_span_raises():
    with pytest.raises(ValueError):
        compute_series([], [], "bad")


def test_compute_series_span_filters_data():
    now = datetime(2026, 1, 10, 16, 0, 0)
    # view items at various days
    view = [
        make_view_item(now - timedelta(days=10), 9000.0, 0.0),
        make_view_item(now - timedelta(days=5), 9500.0, 0.01),
        make_view_item(now - timedelta(days=1), 9800.0, 0.02),
        make_view_item(now, 10000.0, 0.03),
    ]
    spy = [
        (now - timedelta(days=10), 480.0),
        (now - timedelta(days=5), 485.0),
        (now - timedelta(days=1), 490.0),
        (now, 495.0),
    ]
    result = compute_series(view, spy, "1w")
    # only last 7 days, so exclude the first (10 days ago)
    expected_dates = [
        (now - timedelta(days=5)).date().isoformat(),
        (now - timedelta(days=1)).date().isoformat(),
        now.date().isoformat(),
    ]
    assert result["dates"] == expected_dates
    assert result["account_values"] == [9500.0, 9800.0, 10000.0]
    assert result["spy_values"] == [485.0, 490.0, 495.0]


# ---------------------------------------------------------------
# Integration test using the scratch database
# ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_compute_series_with_real_db_rows():
    """Insert pnl rows, read them back as dicts, and compute series."""
    from sqlalchemy import text

    # Insert three rows into pnl (assuming table exists and has these columns)
    insert_sql = text(
        """
        INSERT INTO pnl (
            data_ts, model_ts, as_of_ts, account_value, cash,
            cumulative_return, daily_return, equity, max_drawdown,
            realized_pnl, sharpe_ratio, spy_benchmark_return, turnover,
            unrealized_pnl
        ) VALUES (
            :data_ts, :model_ts, :as_of_ts, :account_value, :cash,
            :cumulative_return, :daily_return, :equity, :max_drawdown,
            :realized_pnl, :sharpe_ratio, :spy_benchmark_return, :turnover,
            :unrealized_pnl
        )
        """
    )
    now = datetime(2026, 1, 10, 16, 0, 0)
    rows = [
        {
            "data_ts": now - timedelta(days=3),
            "model_ts": now - timedelta(days=3),
            "as_of_ts": now - timedelta(days=3),
            "account_value": 9500.0,
            "cash": 5000.0,
            "cumulative_return": 0.01,
            "daily_return": 0.005,
            "equity": 4500.0,
            "max_drawdown": 0.0,
            "realized_pnl": 100.0,
            "sharpe_ratio": 1.0,
            "spy_benchmark_return": 0.005,
            "turnover": 0.1,
            "unrealized_pnl": 200.0,
        },
        {
            "data_ts": now - timedelta(days=2),
            "model_ts": now - timedelta(days=2),
            "as_of_ts": now - timedelta(days=2),
            "account_value": 9700.0,
            "cash": 5200.0,
            "cumulative_return": 0.015,
            "daily_return": 0.005,
            "equity": 4500.0,
            "max_drawdown": -0.005,
            "realized_pnl": 150.0,
            "sharpe_ratio": 1.1,
            "spy_benchmark_return": 0.006,
            "turnover": 0.12,
            "unrealized_pnl": 250.0,
        },
        {
            "data_ts": now - timedelta(days=1),
            "model_ts": now - timedelta(days=1),
            "as_of_ts": now - timedelta(days=1),
            "account_value": 10000.0,
            "cash": 5500.0,
            "cumulative_return": 0.02,
            "daily_return": 0.005,
            "equity": 4500.0,
            "max_drawdown": -0.01,
            "realized_pnl": 200.0,
            "sharpe_ratio": 1.2,
            "spy_benchmark_return": 0.007,
            "turnover": 0.15,
            "unrealized_pnl": 300.0,
        },
    ]

    async with async_session() as db:
        for row in rows:
            await db.execute(insert_sql, row)
        await db.commit()

        # Fetch rows as dicts (using SQLAlchemy row mappings)
        result = await db.execute(
            text("SELECT * FROM pnl ORDER BY as_of_ts")
        )
        fetched_rows = result.mappings().all()
        # Convert each RowMapping to a plain dict
        view = [dict(r) for r in fetched_rows]

    # Provide spy data matching dates
    spy = [
        (rows[0]["as_of_ts"], 480.0),
        (rows[1]["as_of_ts"], 485.0),
        (rows[2]["as_of_ts"], 490.0),
    ]

    # Compute series
    series = compute_series(view, spy, "all")
    assert series["dates"] == [
        rows[0]["as_of_ts"].date().isoformat(),
        rows[1]["as_of_ts"].date().isoformat(),
        rows[2]["as_of_ts"].date().isoformat(),
    ]
    assert series["account_values"] == [9500.0, 9700.0, 10000.0]
    assert series["spy_values"] == [480.0, 485.0, 490.0]
    assert series["net_return"] == 0.02
    assert series["max_drawdown"] == -0.01
    assert series["sharpe_ratio"] == 1.2
    assert series["turnover"] == 0.15
