# tests/test_paper_dashboard_view_builder.py
import pytest
from datetime import datetime, timedelta

from app.modules.paper_dashboard_view_builder import build_dashboard_context


def make_series_item(
    ts: datetime,
    account_value: float,
    cumulative_return: float,
    costs: float = 0.0,
    max_drawdown: float = 0.0,
    sharpe_ratio: float = 0.0,
    turnover: float = 0.0,
    gross_return: float = None,
):
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


def make_summary():
    return {
        "open_positions": [
            {"ticker": "AAPL", "quantity": 10, "side": "long"},
            {"ticker": "MSFT", "quantity": 5, "side": "short"},
        ],
        "recent_orders": [
            {"ticker": "AAPL", "side": "buy", "quantity": 10, "status": "filled"},
        ],
        "fills": [
            {"ticker": "AAPL", "price": 150.0, "quantity": 10, "side": "buy"},
        ],
        "proposals": [
            {
                "id": "prop-1",
                "ticker": "AAPL",
                "side": "buy",
                "quantity": 10,
                "order_type": "market",
                "limit_price": None,
                "rationale": "<script>alert('xss')</script>Buy AAPL because strong earnings",
                "sources": ["<img src=x onerror=alert(1)>Source A", "Source B"],
            }
        ],
    }


def make_audit():
    return [
        {
            "data_ts": datetime(2026, 1, 1),
            "event_type": "proposal",
            "details": "<b>unsafe</b> proposal created",
            "ticker": "AAPL",
        },
        {
            "data_ts": datetime(2026, 1, 2),
            "event_type": "fill",
            "details": "fill executed",
            "ticker": "AAPL",
        },
    ]


def make_spy():
    base = datetime(2026, 1, 1, 16, 0, 0)
    return [
        (base, 500.0),
        (base + timedelta(days=1), 505.0),
    ]


def test_build_dashboard_context_returns_expected_keys():
    series = [
        make_series_item(datetime(2026, 1, 1, 16, 0, 0), 10000.0, 0.01, costs=5.0),
        make_series_item(datetime(2026, 1, 2, 16, 0, 0), 10100.0, 0.02, costs=8.0),
    ]
    summary = make_summary()
    audit = make_audit()
    spy = make_spy()
    ctx = build_dashboard_context(series, summary, audit, spy, "all")

    for key in (
        "span",
        "chart",
        "open_positions",
        "recent_orders",
        "fills",
        "proposals",
        "audit",
        "net_return",
        "gross_return",
        "costs",
        "drawdown",
        "sharpe",
        "turnover",
    ):
        assert key in ctx

    assert ctx["span"] == "all"
    # Chart values come from compute_series
    assert ctx["chart"]["dates"] == ["2026-01-01", "2026-01-02"]
    assert ctx["chart"]["account_values"] == [10000.0, 10100.0]
    assert ctx["chart"]["spy_values"] == [500.0, 505.0]
    assert ctx["net_return"] == pytest.approx(0.02)
    assert ctx["gross_return"] == pytest.approx(0.02 + 8.0)
    assert ctx["costs"] == pytest.approx(8.0)
    assert ctx["drawdown"] == pytest.approx(0.0)
    assert ctx["sharpe"] == pytest.approx(0.0)
    assert ctx["turnover"] == pytest.approx(0.0)


def test_html_escaping_applied_to_all_strings():
    # Build data with malicious strings
    series = [
        make_series_item(datetime(2026, 1, 1), 10000.0, 0.0, costs=0.0)
    ]
    summary = make_summary()
    # Inject XSS into proposal rationale, sources, and audit details
    summary["proposals"][0]["rationale"] = "<script>alert('pwned')</script>"
    summary["proposals"][0]["sources"] = ["<img src=x onerror=alert(1)>", "safe"]
    summary["open_positions"][0]["ticker"] = "<b>EVIL</b>"
    audit = [
        {
            "data_ts": datetime(2026, 1, 1),
            "event_type": "proposal",
            "details": "<iframe src=javascript:alert(1)>",
            "ticker": "<script>bad()</script>",
        }
    ]
    spy = [(datetime(2026, 1, 1), 500.0)]

    ctx = build_dashboard_context(series, summary, audit, spy, "all")

    # Assert escaped strings
    assert ctx["proposals"][0]["rationale"] == "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;"
    assert ctx["proposals"][0]["sources"][0] == "&lt;img src=x onerror=alert(1)&gt;"
    assert ctx["proposals"][0]["sources"][1] == "safe"
    assert ctx["open_positions"][0]["ticker"] == "&lt;b&gt;EVIL&lt;/b&gt;"
    assert ctx["audit"][0]["details"] == "&lt;iframe src=javascript:alert(1)&gt;"
    assert ctx["audit"][0]["ticker"] == "&lt;script&gt;bad()&lt;/script&gt;"


def test_invalid_span_raises_value_error():
    series = [make_series_item(datetime(2026, 1, 1), 10000.0, 0.0)]
    summary = make_summary()
    audit = make_audit()
    spy = make_spy()
    with pytest.raises(ValueError):
        build_dashboard_context(series, summary, audit, spy, "1y")


def test_empty_data_returns_zero_metrics_and_empty_lists():
    series = []
    summary = {"open_positions": [], "recent_orders": [], "fills": [], "proposals": []}
    audit = []
    spy = []
    ctx = build_dashboard_context(series, summary, audit, spy, "all")

    assert ctx["chart"]["dates"] == []
    assert ctx["chart"]["account_values"] == []
    assert ctx["chart"]["spy_values"] == []
    assert ctx["net_return"] == 0.0
    assert ctx["gross_return"] == 0.0
    assert ctx["costs"] == 0.0
    assert ctx["drawdown"] == 0.0
    assert ctx["sharpe"] == "not enough data"   # §17.2045
    assert ctx["turnover"] == 0.0
    assert ctx["open_positions"] == []
    assert ctx["recent_orders"] == []
    assert ctx["fills"] == []
    assert ctx["proposals"] == []
    assert ctx["audit"] == []


def test_span_filters_series_and_spy():
    now = datetime(2026, 1, 10, 16, 0, 0)
    series = [
        make_series_item(now - timedelta(days=10), 9000.0, 0.0),
        make_series_item(now - timedelta(days=5), 9500.0, 0.01),
        make_series_item(now - timedelta(days=1), 9800.0, 0.02),
        make_series_item(now, 10000.0, 0.03),
    ]
    spy = [
        (now - timedelta(days=10), 480.0),
        (now - timedelta(days=5), 485.0),
        (now - timedelta(days=1), 490.0),
        (now, 495.0),
    ]
    summary = make_summary()
    audit = make_audit()
    ctx = build_dashboard_context(series, summary, audit, spy, "1w")

    # Only last 7 days: exclude the 10-day-old point
    expected_dates = [
        (now - timedelta(days=5)).date().isoformat(),
        (now - timedelta(days=1)).date().isoformat(),
        now.date().isoformat(),
    ]
    assert ctx["chart"]["dates"] == expected_dates
    assert ctx["chart"]["account_values"] == [9500.0, 9800.0, 10000.0]
    assert ctx["chart"]["spy_values"] == [485.0, 490.0, 495.0]
    # Metrics from latest filtered item
    assert ctx["net_return"] == pytest.approx(0.03)
