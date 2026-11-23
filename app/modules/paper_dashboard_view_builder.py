# app/modules/paper_dashboard_view_builder.py
import html
from datetime import datetime
from typing import Any, Dict, List, Sequence, Tuple

from app.modules.paper_metrics_computation_module import compute_series

_ALLOWED_SPANS = ("1w", "1m", "3m", "all")


def _escape_str(value: Any) -> Any:
    """Escape a single string value with html.escape; return other values unchanged."""
    if isinstance(value, str):
        return html.escape(value)
    return value


def _escape_recursive(obj: Any) -> Any:
    """Recursively escape all string values inside dicts, lists, and tuples."""
    if isinstance(obj, dict):
        return {key: _escape_recursive(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_escape_recursive(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_escape_recursive(item) for item in obj)
    return _escape_str(obj)


def build_dashboard_context(
    series: List[Dict[str, Any]],
    summary: Dict[str, Any],
    audit: List[Dict[str, Any]],
    spy: Sequence[Tuple[datetime, float]],
    span: str,
) -> Dict[str, Any]:
    """
    Build the template context for the paper-trading dashboard.

    `series` is a list of historical snapshots (each dict must have at least
    `as_of_ts`, `account_value`, `cumulative_return`, `max_drawdown`,
    `sharpe_ratio`, `turnover`; optionally `costs` and `gross_return`).
    `summary` is a dict with keys for non-chart data: `open_positions`,
    `recent_orders`, `fills`, `proposals` (each value a list of dicts).
    `audit` is a list of audit records (dicts).
    `spy` is a list of `(timestamp, price)` tuples for the SPY benchmark.
    `span` must be one of `"1w"`, `"1m"`, `"3m"`, `"all"`.

    Returns a dict ready to pass to `render_template`. All string values
    (operator/model-authored) are HTML-escaped to prevent XSS.
    """
    if span not in _ALLOWED_SPANS:
        raise ValueError(f"Unknown span: {span}")

    # Compute chart data and span-specific metrics using T5's function.
    chart = compute_series(series, spy, span)

    # Build context with top-level keys expected by the template.
    context = {
        "span": span,
        "chart": chart,
        "open_positions": summary.get("open_positions", []),
        "recent_orders": summary.get("recent_orders", []),
        "fills": summary.get("fills", []),
        "proposals": summary.get("proposals", []),
        "audit": audit,
        # Convenience top-level metrics from the chart for the selected span.
        "net_return": chart["net_return"],
        "gross_return": chart["gross_return"],
        "costs": chart["costs"],
        "drawdown": chart["max_drawdown"],
        "sharpe": chart["sharpe_ratio"],
        "turnover": chart["turnover"],
    }

    # Escape every string in the context (numbers and dates pass through).
    return _escape_recursive(context)
