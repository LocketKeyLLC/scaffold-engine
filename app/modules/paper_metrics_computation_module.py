# app/modules/paper_metrics_computation_module.py
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Sequence, Tuple

SPAN_DAYS = {
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "all": None,
}


def _get_attr(item: Any, key: str) -> Any:
    """Return attribute or dict value for item."""
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def filter_by_span(
    items: Sequence[Any],
    span: str,
    timestamp_key: str = "as_of_ts",
) -> List[Any]:
    """
    Filter a list of dicts or objects to the given span.

    items: list of dicts or objects, each having a timestamp accessible by
           timestamp_key (for dicts) or as attribute (for objects).
    span: one of "1w", "1m", "3m", "all".
    Returns the filtered list (same order as input, but only recent items).
    """
    if span not in SPAN_DAYS:
        raise ValueError(f"Unknown span: {span}")

    if not items:
        return []

    days = SPAN_DAYS[span]
    if days is None:
        return list(items)

    # Find the most recent timestamp among all items
    max_ts = None
    for item in items:
        ts = _get_attr(item, timestamp_key)
        if ts is not None:
            if max_ts is None or ts > max_ts:
                max_ts = ts

    if max_ts is None:
        # No timestamps found; return all (or empty?)
        return list(items)

    cutoff = max_ts - timedelta(days=days)
    filtered = []
    for item in items:
        ts = _get_attr(item, timestamp_key)
        if ts is not None and ts >= cutoff:
            filtered.append(item)
    return filtered


def _filter_spy_by_span(
    spy: Sequence[Tuple[datetime, float]], span: str
) -> List[Tuple[datetime, float]]:
    """Filter spy list (timestamp, price) to the given span."""
    if span not in SPAN_DAYS:
        raise ValueError(f"Unknown span: {span}")

    if not spy:
        return []

    days = SPAN_DAYS[span]
    if days is None:
        return list(spy)

    max_ts = max(ts for ts, _ in spy)
    cutoff = max_ts - timedelta(days=days)
    return [(ts, price) for ts, price in spy if ts >= cutoff]


def compute_series(
    view: Sequence[Any],
    spy: Sequence[Tuple[datetime, float]],
    span: str,
) -> Dict[str, Any]:
    """
    Compute dashboard series and summary metrics.

    view: list of dicts or objects with at least keys/attributes:
          as_of_ts, account_value, cumulative_return, max_drawdown,
          sharpe_ratio, turnover, and optionally costs.
          If gross_return is not present, it is computed as
          cumulative_return + costs (if costs present, else cumulative_return).
    spy: list of (timestamp, price) tuples.
    span: "1w", "1m", "3m", or "all".

    Returns dict with keys:
        dates, account_values, spy_values,
        net_return, gross_return, costs, max_drawdown,
        sharpe_ratio, turnover.
    """
    if span not in SPAN_DAYS:
        raise ValueError(f"Unknown span: {span}")

    # Filter both series
    filtered_view = filter_by_span(view, span, timestamp_key="as_of_ts")
    filtered_spy = _filter_spy_by_span(spy, span)

    if not filtered_view or not filtered_spy:
        # No data for requested span; return empty series and zeros
        return {
            "dates": [],
            "account_values": [],
            "spy_values": [],
            "net_return": 0.0,
            "gross_return": 0.0,
            "costs": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "turnover": 0.0,
        }

    # Sort by timestamp
    filtered_view.sort(key=lambda x: _get_attr(x, "as_of_ts"))
    filtered_spy.sort(key=lambda x: x[0])

    # Build date -> view row mapping (latest per date)
    view_by_date: Dict[date, Any] = {}
    for item in filtered_view:
        ts = _get_attr(item, "as_of_ts")
        if ts is None:
            continue
        d = ts.date()
        view_by_date[d] = item  # later items overwrite earlier (sorted)

    # Build date -> spy price mapping (latest per date)
    spy_by_date: Dict[date, float] = {}
    for ts, price in filtered_spy:
        d = ts.date()
        spy_by_date[d] = price

    # Align dates: intersection of both sets
    common_dates = sorted(set(view_by_date.keys()) & set(spy_by_date.keys()))
    if not common_dates:
        return {
            "dates": [],
            "account_values": [],
            "spy_values": [],
            "net_return": 0.0,
            "gross_return": 0.0,
            "costs": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": 0.0,
            "turnover": 0.0,
        }

    dates = [d.isoformat() for d in common_dates]
    account_values = [float(_get_attr(view_by_date[d], "account_value")) for d in common_dates]
    spy_values = [float(spy_by_date[d]) for d in common_dates]

    # Summary metrics from the latest view row among filtered (last date)
    latest_item = view_by_date[common_dates[-1]]
    net_return = float(_get_attr(latest_item, "cumulative_return") or 0.0)
    costs = float(_get_attr(latest_item, "costs") or 0.0)
    gross_return = _get_attr(latest_item, "gross_return")
    if gross_return is None:
        gross_return = net_return + costs
    else:
        gross_return = float(gross_return)
    max_drawdown = float(_get_attr(latest_item, "max_drawdown") or 0.0)
    sharpe_ratio = float(_get_attr(latest_item, "sharpe_ratio") or 0.0)
    turnover = float(_get_attr(latest_item, "turnover") or 0.0)

    return {
        "dates": dates,
        "account_values": account_values,
        "spy_values": spy_values,
        "net_return": net_return,
        "gross_return": gross_return,
        "costs": costs,
        "max_drawdown": max_drawdown,
        "sharpe_ratio": sharpe_ratio,
        "turnover": turnover,
    }
