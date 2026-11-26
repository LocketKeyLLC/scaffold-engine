# app/modules/paper_wire_engine_entry_module.py
"""Wire the paper-trading lab modules into the scaffold engine."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from sqlalchemy import text

from app.authz import get_principal
from app.database import async_session
from app.modules.paper_daily_signal_proposal_batch import run_on_demand_batch
from app.modules.paper_dashboard_view_builder import build_dashboard_context
from app.modules.paper_enforce_read_only_data import read_only_session
from app.modules.paper_entry_approval import run_approval_entry_point
from app.modules.paper_entry_backtesting import run_backtesting_entry_point
from app.modules.paper_execution_entry_point import run_execution_entry_point
from app.modules.paper_fill_tracker import run_fill_tracker
from app.modules.paper_lab_schedule import (
    DAILY_INGEST_JOB_ID,
    DAILY_INGEST_TRIGGER,
    EOD_SNAPSHOT_JOB_ID,
    EOD_SNAPSHOT_TRIGGER,
    WEEKLY_JOB_ID,
    WEEKLY_TRIGGER,
    run_daily_ingestion,
    run_eod_snapshot,
    run_scheduled_daily_batch,
    run_weekly,
)
from app.modules.paper_operator_view import get_operator_view
from app.modules.paper_serve_static_asset_under import install as install_static_assets
from app.modules.paper_serve_static_asset_under import vendor_url, vendored_files
from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark
from app.scheduler import get_scheduler

logger = logging.getLogger("scaffold")

LAB_SLUG = "paper"
ROUTER_PREFIX = f"/labs/{LAB_SLUG}"
DAILY_BATCH_JOB_ID = "paper_daily_batch"
DAILY_BATCH_HOUR = 13
DAILY_BATCH_MINUTE = 0
DASHBOARD_SPANS = ("1w", "1m", "3m", "all")
DASHBOARD_TEMPLATE = "paper/dashboard.html"

SPY_SECRET_NAMES = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
SPY_UNAVAILABLE_REASON = "the operator secret is not stored yet"
SPY_UNAVAILABLE_NOTE = (
    "The SPY benchmark is unavailable: the operator secret is not stored yet "
    "(ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY). Store them on the Machines page "
    "\u2192 Secrets; the rest of the page is unaffected."
)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_SERIES_SQL = text(
    """
    SELECT as_of_ts, account_value, cumulative_return,
           max_drawdown, sharpe_ratio, turnover
    FROM pnl
    ORDER BY as_of_ts
    """
)
_FILL_COST_SQL = text("SELECT filled_ts, commission FROM fill")


def _as_utc(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _raw_session(ro: Any) -> Any:
    """The plain AsyncSession under a read-only one, however that module exposes it."""
    raw = getattr(ro, "raw", None)
    return raw() if callable(raw) else raw


def _attach_costs(series: List[Dict[str, Any]], fill_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    fills = [row for row in fill_rows if row.get("filled_ts") is not None]
    fills.sort(key=lambda row: _as_utc(row["filled_ts"]))
    index = 0
    cumulative = 0.0
    for item in series:
        stamp = _as_utc(item.get("as_of_ts"))
        while index < len(fills) and stamp is not None and _as_utc(fills[index]["filled_ts"]) <= stamp:
            cumulative += float(fills[index].get("commission") or 0.0)
            index += 1
        item["costs"] = cumulative
    return series


async def _load_series() -> List[Dict[str, Any]]:
    """Read the pnl snapshots, oldest first, with costs added from fills, via the read-only session."""
    async with read_only_session() as ro:
        series_rows = await ro.fetch_all(_SERIES_SQL)
        fill_rows = await ro.fetch_all(_FILL_COST_SQL)
    return _attach_costs(series_rows, fill_rows)


def _series_window(series: List[Dict[str, Any]]) -> tuple:
    stamps = [_as_utc(item["as_of_ts"]) for item in series if item.get("as_of_ts") is not None]
    if stamps:
        return min(stamps), max(stamps)
    now = datetime.now(timezone.utc)
    return now - timedelta(days=7), now


def _fmt_number(value: Any, spec: str = "{:.2f}") -> str:
    if value is None:
        return "\u2014"
    try:
        return spec.format(value)
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "\u2014"
    try:
        return "{:+.2f}%".format(float(value) * 100.0)
    except (TypeError, ValueError):
        return str(value)


def _one_line(value: Any, limit: int = 120) -> str:
    if not value:
        return ""
    collapsed = " ".join(str(value).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "\u2026"


def _sources(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        value = [value]
    items = "".join(f"<li>{item}</li>" for item in value)
    return f'<ul class="sources">{items}</ul>'


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{empty}</p>'
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _chart_script() -> str:
    """Point the page at the lab's own vendored Chart.js bundle; never a CDN."""
    try:
        names = vendored_files()
    except Exception:  # the manifest is absent until the delivery flow writes it
        return ""
    for name in names:
        if "chart" in str(name).lower():
            try:
                return f'<script src="{vendor_url(str(name))}" defer></script>'
            except (ValueError, OSError):
                return ""
    return ""


_DASHBOARD_FALLBACK = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Paper trading \u2014 dashboard</title>
<style>
 :root { color-scheme: light dark; --line:#e2e5ea; --muted:#5b6470; }
 body { margin:0; padding:24px 16px 56px; font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
 h1 { font-size:22px; margin:0 0 10px; }
 h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:24px 0 8px; }
 .spans a { display:inline-block; padding:4px 10px; margin-right:4px; font-size:13px; border:1px solid var(--line); border-radius:6px; text-decoration:none; color:inherit; }
 .spans a.is-active { border-color:#2f6fed; color:#2f6fed; font-weight:600; }
 .kpis { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:10px; }
 .kpi { border:1px solid var(--line); border-radius:8px; padding:8px 12px; }
 .kpi-label { display:block; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
 .kpi-value { display:block; font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; }
 table { width:100%; border-collapse:collapse; font-size:13.5px; }
 th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
 th { font-size:11.5px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
 .empty, .note { color:var(--muted); font-size:13px; margin:0; }
 .unavailable { border:1px solid #d9a400; background:rgba(217,164,0,.10); border-radius:8px; padding:8px 12px; font-size:13px; margin:0 0 8px; }
 .proposal { border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin-bottom:8px; }
 .proposal summary { cursor:pointer; }
 .sources { margin:6px 0 0; padding-left:18px; font-size:13px; word-break:break-word; }
 .chart { margin:0 0 12px; }
</style>
</head>
<body>
<h1>Paper trading \u2014 dashboard</h1>
<nav class="spans">$spans</nav>
<p class="note">Built-in page: the dashboard template $template is not installed in this engine.</p>
<h2>Returns after costs</h2>
<section class="kpis">$kpis</section>
<h2>Account value vs SPY</h2>
$spy_note
<figure class="chart"><canvas id="account-value-vs-spy" data-chart-lib="chart.js"></canvas></figure>
$chart_script
$table
<h2>Open positions</h2>
$positions
<h2>Recent orders</h2>
$orders
<h2>Fills</h2>
$fills
<h2>Proposals</h2>
$proposals
<h2>Audit trail</h2>
$audit
</body>
</html>"""
)


def _builtin_dashboard(context: Dict[str, Any]) -> str:
    span = context.get("span", "all")
    chart: Dict[str, Any] = {}
    nested = context.get("chart")
    if isinstance(nested, dict):
        chart = nested
    if not chart.get("dates") and isinstance(context.get("dates"), list):
        chart = context
    dates = list(chart.get("dates") or [])
    account_values = list(chart.get("account_values") or [])
    spy_values = list(chart.get("spy_values") or [])

    spans = "".join(
        '<a class="{active}" href="?span={name}">{name}</a>'.format(
            active="is-active" if name == span else "", name=name
        )
        for name in DASHBOARD_SPANS
    )

    kpi_list = (
        ("Net return", _fmt_pct(context.get("net_return"))),
        ("Gross return", _fmt_pct(context.get("gross_return"))),
        ("Costs", _fmt_number(context.get("costs"))),
        ("Max drawdown", _fmt_pct(context.get("max_drawdown", context.get("drawdown")))),
        ("Sharpe ratio", _fmt_number(context.get("sharpe_ratio", context.get("sharpe")))),
        ("Turnover", _fmt_number(context.get("turnover"))),
    )
    kpis = "".join(
        '<div class="kpi"><span class="kpi-label">{}</span><span class="kpi-value">{}</span></div>'.format(l, v)
        for l, v in kpi_list
    )

    series_rows = [
        (str(day), _fmt_number(value, "{:,.2f}"),
         _fmt_number(spy_values[index], "{:.2f}") if index < len(spy_values) else "\u2014")
        for index, (day, value) in enumerate(zip(dates, account_values, strict=False))
    ]

    position_rows = [
        (str(row.get("ticker", "")), str(row.get("side", "")),
         _fmt_number(row.get("quantity"), "{:g}"), _fmt_number(row.get("market_value"), "{:,.2f}"))
        for row in context.get("open_positions") or [] if isinstance(row, dict)
    ]
    order_rows = [
        (str(row.get("ticker", "")), str(row.get("side", "")),
         _fmt_number(row.get("quantity"), "{:g}"), str(row.get("status") or row.get("order_type") or ""))
        for row in context.get("recent_orders") or [] if isinstance(row, dict)
    ]
    fill_rows = [
        (str(row.get("ticker", "")), str(row.get("side", "")),
         _fmt_number(row.get("quantity"), "{:g}"), _fmt_number(row.get("price"), "{:,.2f}"))
        for row in context.get("fills") or [] if isinstance(row, dict)
    ]

    proposal_html = "".join(
        '<details class="proposal"><summary><strong>{ticker}</strong> {side} {qty} &middot; {summary}</summary>'
        "<p>{rationale}</p>{sources}</details>".format(
            ticker=row.get("ticker", ""), side=row.get("side", ""),
            qty=_fmt_number(row.get("quantity"), "{:g}"),
            summary=_one_line(row.get("rationale")), rationale=row.get("rationale") or "",
            sources=_sources(row.get("sources")),
        )
        for row in context.get("proposals") or [] if isinstance(row, dict)
    )

    audit_rows = [
        (str(row.get("data_ts", "")), str(row.get("event_type", "")),
         str(row.get("ticker", "")), str(row.get("details", "")))
        for row in context.get("audit") or [] if isinstance(row, dict)
    ]

    spy_note = ""
    if context.get("spy_unavailable"):
        spy_note = '<p class="unavailable" data-unavailable="spy">{}</p>'.format(
            context.get("spy_unavailable_note") or SPY_UNAVAILABLE_NOTE
        )

    return _DASHBOARD_FALLBACK.substitute(
        spans=spans,
        template=DASHBOARD_TEMPLATE,
        kpis=kpis,
        spy_note=spy_note,
        chart_script=_chart_script(),
        table=_table(("Date", "Account value", "SPY"), series_rows, "No snapshots recorded in this span yet."),
        positions=_table(("Ticker", "Side", "Quantity", "Market value"), position_rows, "No open positions."),
        orders=_table(("Ticker", "Side", "Quantity", "Status"), order_rows, "No recent orders."),
        fills=_table(("Ticker", "Side", "Quantity", "Price"), fill_rows, "No fills recorded."),
        proposals=proposal_html or '<p class="empty">No proposals on file.</p>',
        audit=_table(("Time", "Event", "Ticker", "Details"), audit_rows, "The audit trail is empty."),
    )


def _render_dashboard(request: Request, context: Dict[str, Any]) -> HTMLResponse:
    try:
        return templates.TemplateResponse(request, DASHBOARD_TEMPLATE, {"request": request, **context})
    except TemplateNotFound:
        logger.warning("dashboard template %s not found under %s; serving the built-in page",
                       DASHBOARD_TEMPLATE, _TEMPLATES_DIR)
        return HTMLResponse(_builtin_dashboard(context))


def _make_router() -> APIRouter:
    router = APIRouter(prefix=ROUTER_PREFIX, tags=["paper"])

    @router.post("/daily-batch")
    async def daily_batch(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        # §17.2041 -- the same run the scheduler starts: day-start equity, then the batch unless paused
        return await run_scheduled_daily_batch()

    @router.post("/on-demand-batch")
    async def on_demand_batch(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        return await run_on_demand_batch()

    @router.post("/approval")
    async def approval(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        return await run_approval_entry_point()

    @router.post("/backtesting")
    async def backtesting(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        return await run_backtesting_entry_point()

    @router.post("/execution")
    async def execution(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        async with async_session() as db:
            return await run_execution_entry_point(
                db=db,
                operator_id=_principal.identity,
                timestamp=datetime.now(timezone.utc),
            )

    @router.get("/view")
    async def view(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        return await get_operator_view()

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        span: str = Query("all"),
        _principal: Any = Depends(get_principal),
    ) -> HTMLResponse:
        if span not in DASHBOARD_SPANS:
            raise HTTPException(status_code=400, detail=f"Unknown span: {span}")

        series = await _load_series()

        view_data = await get_operator_view()
        if not isinstance(view_data, dict):
            view_data = {}
        summary: Dict[str, Any] = {
            key: view_data.get(key) or []
            for key in ("open_positions", "recent_orders", "fills", "proposals")
        }
        raw_audit: Any = (
            view_data.get("audit")
            or view_data.get("audit_entries")
            or view_data.get("audit_trail")
            or []
        )
        audit: List[Dict[str, Any]] = raw_audit if isinstance(raw_audit, list) else []

        start, end = _series_window(series)

        # The SPY benchmark needs the operator's Alpaca keys through
        # paper_alpaca_paper_client. Until they are stored it raises; the rest of the
        # page must still render, so the failure is contained here and reported as
        # "this part is unavailable" (§17.2003).
        spy: Any = []
        spy_unavailable = False
        try:
            async with read_only_session() as ro:
                spy = await get_spy_benchmark(start_date=start, end_date=end, db=_raw_session(ro))
        except Exception:
            logger.info("SPY benchmark unavailable on the dashboard", exc_info=True)
            spy = []
            spy_unavailable = True
        if not isinstance(spy, list):
            spy = []

        context = build_dashboard_context(series, summary, audit, spy, span)
        if not isinstance(context, dict):
            context = {}
        if spy_unavailable:
            context["spy_unavailable"] = True
            context["spy_unavailable_note"] = SPY_UNAVAILABLE_NOTE
            context["spy_unavailable_reason"] = SPY_UNAVAILABLE_REASON
            context["spy_unavailable_secrets"] = list(SPY_SECRET_NAMES)
        return _render_dashboard(request, context)

    return router


def _start_stream_consumer() -> Optional[asyncio.Task]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(run_fill_tracker())   # §17.2042 — supervised: no keys is a logged wait, not a crash


def install(app: FastAPI) -> None:
    scheduler = get_scheduler()
    if scheduler is not None:
        # §17.2041 -- the daily batch first records the day-start equity it reads and skips while the strategy is
        # paused; the ingesters fill the point-in-time store two hours before it; the snapshot and the weekly
        # re-evaluation run on their own times (paper_lab_schedule).
        scheduler.add_job(
            run_scheduled_daily_batch,
            trigger="cron",
            hour=DAILY_BATCH_HOUR,
            minute=DAILY_BATCH_MINUTE,
            id=DAILY_BATCH_JOB_ID,
            replace_existing=True,
        )
        for run, job_id, trigger in (
            (run_daily_ingestion, DAILY_INGEST_JOB_ID, DAILY_INGEST_TRIGGER),
            (run_eod_snapshot, EOD_SNAPSHOT_JOB_ID, EOD_SNAPSHOT_TRIGGER),
            (run_weekly, WEEKLY_JOB_ID, WEEKLY_TRIGGER),
        ):
            scheduler.add_job(run, id=job_id, replace_existing=True, **trigger)

    app.include_router(_make_router())
    install_static_assets(app)
    _start_stream_consumer()
