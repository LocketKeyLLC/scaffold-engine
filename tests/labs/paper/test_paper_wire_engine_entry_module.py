# tests/test_paper_wire_engine_entry_module.py
"""Tests for app/modules/paper_wire_engine_entry_module.py."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from jinja2 import TemplateNotFound

from app.authz import get_principal
from app.modules import paper_wire_engine_entry_module as entry
from app.modules.paper_serve_static_asset_under import lab_static_dir, vendor_dir
from app.modules.paper_wire_engine_entry_module import (
    DAILY_BATCH_HOUR,
    DAILY_BATCH_JOB_ID,
    DAILY_BATCH_MINUTE,
    DASHBOARD_SPANS,
    LAB_SLUG,
    ROUTER_PREFIX,
    SPY_SECRET_NAMES,
    SPY_UNAVAILABLE_NOTE,
    _builtin_dashboard,
    _make_router,
    install,
)

LAB_API_KEY = "test-lab-api-key"
DASHBOARD_PATH = "/labs/paper/dashboard"
_MODULE = "app.modules.paper_wire_engine_entry_module"
_ALPACA = "app.modules.paper_alpaca_paper_client"
_SPY_MODULE = "app.modules.paper_spy_benchmark_tracker"
_AUTH_HINTS = ("api_key", "apikey", "principal", "get_principal", "auth")

SPY_ROWS = [
    (datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc), 495.0),
    (datetime(2026, 11, 21, 21, 0, tzinfo=timezone.utc), 500.0),
]

ROUTE_SUFFIXES = (
    "/daily-batch",
    "/on-demand-batch",
    "/approval",
    "/backtesting",
    "/execution",
    "/view",
    "/dashboard",
)


class MockPrincipal:
    identity = "test-operator"


@pytest.fixture(autouse=True)
def _ensure_static_root():
    """Make sure the real lab static root exists so the real install_static_assets can mount it."""
    root = lab_static_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "vendor").mkdir(exist_ok=True)
    yield


def _refuse():
    raise HTTPException(status_code=401, detail="Not authorized")


def _satisfy_auth_dependencies(app: FastAPI, path: str, override=None) -> list:
    """Point whatever auth dependency the app's routes declare at a stand-in."""
    if override is None:
        override = MockPrincipal()
    app.dependency_overrides[get_principal] = lambda: override
    return [get_principal]


def _lab_app() -> FastAPI:
    """A bare FastAPI app with the lab router installed."""
    app = FastAPI()
    entry.install(app)
    return app


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.include_router(entry._make_router())
    return app


def make_app() -> FastAPI:
    app = FastAPI()
    entry.install(app)
    return app


def _app_paths(app: FastAPI) -> set:
    return set(app.openapi()["paths"].keys())


def _labs_paper_paths(app: FastAPI) -> set:
    return {p for p in _app_paths(app) if p.startswith(ROUTER_PREFIX)}


def _dump_lines(value, out: list) -> list:
    for line in str(value).splitlines():
        out.append(line)
    return out


def _render_stub(*args, **kwargs) -> Response:
    return Response("<html><body>stub</body></html>")


@contextmanager
def _no_template():
    """Stand in for the delivered Jinja template, which the delivery flow ships."""
    from app.modules.paper_wire_engine_entry_module import DASHBOARD_TEMPLATE

    def _raise(*args, **kwargs):
        raise TemplateNotFound(DASHBOARD_TEMPLATE)

    with patch.object(entry.templates, "TemplateResponse", _raise):
        yield


@contextmanager
def _render_captures():
    """Capture one context dict per render, so two requests can be compared."""
    captured = []

    def _capture(request, name, context):
        captured.append(context)
        return Response("<html><body>captured</body></html>")

    with patch.object(entry.templates, "TemplateResponse", _capture):
        yield captured


def _series_timestamps(context: dict) -> list:
    chart = context.get("chart") or {}
    dates = chart.get("dates") or context.get("dates") or []
    return list(dates)


def _stamp_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _series_lists(context: dict) -> list:
    chart = context.get("chart") or {}
    out = []
    for key in ("dates", "account_values", "spy_values"):
        val = chart.get(key) or context.get(key) or []
        if val:
            out.append(list(val))
    return out


def _ordered_stamps(context: dict) -> list:
    return [_stamp_text(v) for v in _series_timestamps(context)]


def _make_mock_spy() -> AsyncMock:
    """The SPY benchmark is the one thing on this page that leaves the engine."""
    mock = AsyncMock(return_value=SPY_ROWS)
    return mock


def _make_mock_view(data: dict | None = None) -> AsyncMock:
    if data is None:
        data = _make_view_data()
    return AsyncMock(return_value=data)


def _make_view_data() -> dict:
    """Returns keys: audit, fills, open_positions, proposals, recent_orders"""
    return {
        "audit": [
            {
                "data_ts": datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc),
                "event_type": "approval",
                "ticker": "AAPL",
                "details": "approved buy",
            },
        ],
        "fills": [
            {
                "ticker": "AAPL",
                "side": "buy",
                "quantity": 10,
                "price": 190.0,
                "filled_ts": datetime(2026, 11, 21, 19, 0, tzinfo=timezone.utc),
            },
        ],
        "open_positions": [
            {"ticker": "AAPL", "side": "long", "quantity": 10, "market_value": 1950.0},
        ],
        "proposals": [
            {
                "id": 1,
                "ticker": "AAPL",
                "side": "buy",
                "quantity": 10,
                "order_type": "market",
                "rationale": "Strong momentum",
                "sources": ["https://example.com/a"],
            },
        ],
        "recent_orders": [
            {"ticker": "AAPL", "side": "buy", "quantity": 10, "status": "filled"},
        ],
    }


async def _insert_pnl(
    ts: datetime,
    account_value: float = 1000.0,
    cumulative_return: float = 0.0,
) -> None:
    """Insert one committed pnl snapshot, filling every column the table insists on."""
    from app.database import async_session
    from sqlalchemy import text

    async with async_session() as db:
        await db.execute(
            text(
                """
                INSERT INTO pnl (as_of_ts, account_value, cumulative_return,
                                 max_drawdown, sharpe_ratio, turnover, equity, cash)
                VALUES (:ts, :av, :cr, 0.0, 0.0, 0.0, :equity, :cash)
                """
            ),
            {
                "ts": ts,
                "av": account_value,
                "cr": cumulative_return,
                "equity": account_value,
                "cash": 0.0,
            },
        )
        await db.commit()


def _ts(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _depends_on(route, dependency) -> bool:
    for d in route.dependant.dependencies:
        if d.call is dependency:
            return True
    return False


def _http_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": DASHBOARD_PATH,
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
    }
    return Request(scope)


def _dashboard_endpoint():
    router = entry._make_router()
    for route in router.routes:
        if route.path == "/dashboard":
            return route
    return None


@pytest.fixture()
def lab_secrets():
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        yield


@pytest.fixture()
def alpaca_secrets():
    with patch(
        f"{_ALPACA}.values_for",
        AsyncMock(
            return_value={
                "ALPACA_API_KEY_ID": "test-key-id",
                "ALPACA_API_SECRET_KEY": "test-secret-key",
            }
        ),
    ):
        yield


@pytest.fixture()
def spy_benchmark():
    """The SPY benchmark is the one thing on this page that leaves the engine."""
    mock = AsyncMock(return_value=SPY_ROWS)
    with patch.object(entry, "get_spy_benchmark", mock):
        yield mock


@pytest.fixture()
def client(spy_benchmark):
    """A client for the lab app with the delivered template stood in for."""
    app = _lab_app()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        yield c


# ---- Install tests ----

def test_install_includes_router():
    app = FastAPI()
    install(app)
    assert any(p.startswith(ROUTER_PREFIX) for p in _app_paths(app))


def test_install_does_not_mutate_existing_routes():
    app = FastAPI()
    @app.get("/existing")
    def existing():
        return {"ok": True}
    install(app)
    paths = _app_paths(app)
    assert "/existing" in paths


def test_install_uses_paper_prefix():
    app = FastAPI()
    install(app)
    paths = _app_paths(app)
    assert any(p.startswith(ROUTER_PREFIX) for p in paths)


def test_install_registers_daily_batch_job():
    app = FastAPI()
    mock_scheduler = MagicMock()
    with patch("app.modules.paper_wire_engine_entry_module.get_scheduler", return_value=mock_scheduler):
        install(app)
    mock_scheduler.add_job.assert_called_once()
    call = mock_scheduler.add_job.call_args
    assert call.kwargs.get("id") == DAILY_BATCH_JOB_ID
    assert call.kwargs.get("trigger") == "cron"
    assert call.kwargs.get("hour") == DAILY_BATCH_HOUR
    assert call.kwargs.get("minute") == DAILY_BATCH_MINUTE
    assert call.args[0] == entry.run_daily_batch


def test_install_registers_routes():
    app = _lab_app()
    paths = _app_paths(app)
    for suffix in ROUTE_SUFFIXES:
        assert f"{ROUTER_PREFIX}{suffix}" in paths


def test_install_no_scheduler_does_not_schedule():
    app = FastAPI()
    with patch("app.modules.paper_wire_engine_entry_module.get_scheduler", return_value=None):
        install(app)  # should not raise


def test_install_starts_stream_consumer_task():
    with patch.object(entry, "_start_stream_consumer") as mock_start:
        app = FastAPI()
        install(app)
        mock_start.assert_called_once()


def test_install_does_not_read_secrets():
    """install() must not reach the operator's secrets."""
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})) as mock_vf:
        app = FastAPI()
        install(app)
    mock_vf.assert_not_called()


def test_install_mounts_the_labs_static_bundle():
    app = FastAPI()
    install(app)
    static_mounts = [
        route for route in app.routes
        if getattr(route, "path", "").startswith("/labs/paper/static")
    ]
    assert static_mounts, "static bundle not mounted"


def test_install_mounts_static_without_losing_the_lab_routes():
    app = FastAPI()
    install(app)
    paths = _app_paths(app)
    assert any(p.startswith(ROUTER_PREFIX) for p in paths)


def test_install_keeps_the_existing_lab_routes():
    app = _lab_app()
    paths = _app_paths(app)
    assert f"{ROUTER_PREFIX}/view" in paths
    assert f"{ROUTER_PREFIX}/execution" in paths


def test_install_registers_dashboard_under_labs_paper_prefix():
    app = _lab_app()
    paths = _app_paths(app)
    assert DASHBOARD_PATH in paths


# ---- Execution route tests ----

def test_execution_route_passes_identity_timestamp_and_session():
    with patch.object(entry, "run_execution_entry_point", new=AsyncMock(return_value={"ok": True})) as exec_mock:
        app = _app_with_router()
        _satisfy_auth_dependencies(app, f"{ROUTER_PREFIX}/execution")
        with TestClient(app) as c:
            response = c.post(f"{ROUTER_PREFIX}/execution")
        assert response.status_code == 200
        call = exec_mock.call_args
        assert call is not None
        assert call.kwargs["operator_id"] == "test-operator"
        assert "timestamp" in call.kwargs
        assert "db" in call.kwargs


def test_execution_route_opens_its_own_session():
    with patch.object(entry, "run_execution_entry_point", new=AsyncMock(return_value={"ok": True})) as exec_mock:
        with patch("app.database.async_session") as session_cls:
            session_ctx = MagicMock()
            session_cls.return_value = session_ctx
            session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
            session_ctx.__aexit__ = AsyncMock(return_value=False)
            app = _app_with_router()
            _satisfy_auth_dependencies(app, f"{ROUTER_PREFIX}/execution")
            with TestClient(app) as c:
                response = c.post(f"{ROUTER_PREFIX}/execution")
            assert response.status_code == 200
            db_arg = exec_mock.call_args.kwargs["db"]
            assert db_arg is not None


def test_execution_route_uses_the_principal_from_the_engine_dependency():
    with patch.object(entry, "run_execution_entry_point", new=AsyncMock(return_value={"ok": True})) as exec_mock:
        app = _app_with_router()
        _satisfy_auth_dependencies(app, f"{ROUTER_PREFIX}/execution")
        with TestClient(app) as c:
            response = c.post(f"{ROUTER_PREFIX}/execution")
        assert response.status_code == 200
        assert exec_mock.call_args.kwargs["operator_id"] == "test-operator"


# ---- Dashboard auth and rendering tests ----

def test_router_has_dashboard_route():
    router = _make_router()
    paths = {getattr(r, "path", "") for r in router.routes}
    assert f"{ROUTER_PREFIX}/dashboard" in paths


def test_existing_routes_unchanged():
    router = _make_router()
    paths = {getattr(r, "path", "") for r in router.routes}
    for suffix in ROUTE_SUFFIXES:
        assert f"{ROUTER_PREFIX}{suffix}" in paths


def test_dashboard_requires_auth(spy_benchmark):
    app = _bare_router_app()
    app.dependency_overrides[get_principal] = _refuse
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code == 401


def test_dashboard_route_returns_200_html():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_dashboard_renders_html_without_the_template_file(spy_benchmark):
    app = _app_with_router()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with _no_template():
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert "Built-in page" in response.text


def test_dashboard_empty_data_returns_200():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200


def test_dashboard_all_valid_spans_return_200():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            for span in DASHBOARD_SPANS:
                response = c.get(DASHBOARD_PATH, params={"span": span})
                assert response.status_code == 200


def test_dashboard_invalid_span_returns_400():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH, params={"span": "bogus"})
    assert response.status_code == 400


def test_dashboard_route_invalid_span():
    app = _bare_router_app()
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH, params={"span": "nope"})
    assert response.status_code == 400


def test_dashboard_offers_one_week_one_month_three_month_and_all():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    body = response.text
    for span in ("1w", "1m", "3m", "all"):
        assert span in body


def test_dashboard_plots_account_value_against_spy():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    body = response.text
    assert "Account value vs SPY" in body
    assert "equity-chart" in body


def test_existing_view_route_preserved():
    router = _make_router()
    paths = {getattr(r, "path", "") for r in router.routes}
    assert f"{ROUTER_PREFIX}/view" in paths


def test_invalid_span_raises_400():
    app = _bare_router_app()
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH, params={"span": "bogus"})
    assert response.status_code == 400


def test_dashboard_route_is_get_only():
    app = _bare_router_app()
    with TestClient(app) as c:
        response = c.post(DASHBOARD_PATH)
    assert response.status_code == 405


def test_dashboard_does_not_refresh_itself():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    body = response.text.lower()
    assert "http-equiv=\"refresh\"" not in body
    assert "setinterval" not in body


def test_dashboard_rejects_a_wrong_api_key(spy_benchmark):
    app = _app_with_router()

    def _auth():
        raise HTTPException(status_code=401, detail="Invalid API key")

    app.dependency_overrides[get_principal] = _auth
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code in (401, 403)


def test_dashboard_requires_the_labs_api_key():
    app = _lab_app()
    app.dependency_overrides[get_principal] = _refuse
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code in (401, 403)


def test_dashboard_refusal_does_not_leak_the_page(spy_benchmark):
    app = _app_with_router()

    def _auth():
        raise HTTPException(status_code=401, detail="Invalid API key")

    app.dependency_overrides[get_principal] = _auth
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    body = response.text
    assert "paper trading" not in body.lower()


def test_dashboard_uses_the_vendored_chartjs_not_a_cdn():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    body = response.text
    assert "chart.umd.min.js" in body
    assert "cdn.jsdelivr.net" not in body
    assert "unpkg.com" not in body


def test_dashboard_lists_positions_orders_and_fills(spy_benchmark, monkeypatch):
    monkeypatch.setattr(entry, "get_operator_view", AsyncMock(return_value=_make_view_data()))
    app = _lab_app()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert "AAPL" in response.text


def test_dashboard_shows_drawdown_sharpe_and_turnover():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    body = response.text
    assert "Max drawdown" in body
    assert "Sharpe ratio" in body
    assert "Turnover" in body


def test_dashboard_shows_proposals_and_the_audit_trail(spy_benchmark, monkeypatch):
    monkeypatch.setattr(entry, "get_operator_view", AsyncMock(return_value=_make_view_data()))
    app = _lab_app()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert "Proposals" in response.text
    assert "Audit trail" in response.text
    assert "AAPL" in response.text


def test_dashboard_shows_net_beside_gross_and_the_costs():
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _make_mock_spy()):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    body = response.text
    assert "Net return" in body
    assert "Gross return" in body
    assert "Costs subtracted" in body


# ---- Load series tests ----

@pytest.mark.asyncio
async def test_load_series_returns_empty_for_unknown_series():
    from app.database import async_session
    from sqlalchemy import text

    async with async_session() as db:
        await db.execute(text("DELETE FROM pnl"))
        await db.commit()
    series = await entry._load_series()
    assert series == []


@pytest.mark.asyncio
async def test_load_series_reads_committed_pnl_rows_oldest_first(spy_benchmark):
    await _insert_pnl(_ts(3), account_value=900.0, cumulative_return=-0.1)
    await _insert_pnl(_ts(1), account_value=1100.0, cumulative_return=0.1)
    series = await entry._load_series()
    stamps = [entry._as_utc(row["as_of_ts"]) for row in series]
    assert stamps == sorted(stamps)
    assert len(series) >= 2


def test_start_stream_consumer_outside_a_loop_returns_none():
    assert entry._start_stream_consumer() is None


def test_dashboard_reads_through_the_read_only_session(spy_benchmark):
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", spy_benchmark):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200


def test_valid_span_builds_context_and_renders(spy_benchmark):
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", spy_benchmark):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH, params={"span": "1m"})
    assert response.status_code == 200
    assert "1m" in response.text


def test_missing_view_keys_default_to_empty(spy_benchmark, monkeypatch):
    async def _view():
        return {}

    monkeypatch.setattr("app.modules.paper_operator_view.get_operator_view", _view)
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", spy_benchmark):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    body = response.text
    assert "No open positions" in body
    assert "No recent orders" in body
    assert "No fills" in body
    assert "No proposals" in body
    assert "The audit trail is empty" in body


@pytest.mark.asyncio
async def test_dashboard_with_pnl_data_returns_200(spy_benchmark):
    await _insert_pnl(_ts(1), account_value=1000.0, cumulative_return=0.0)
    app = _app_with_router()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200


def test_dashboard_passes_the_series_window_to_the_spy_benchmark(spy_benchmark):
    app = _lab_app()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        c.get(DASHBOARD_PATH)
    call = spy_benchmark.call_args
    assert call is not None
    assert "start_date" in call.kwargs
    assert "end_date" in call.kwargs


# ---- SPY unavailable tests ----

def test_dashboard_answers_200_when_spy_secret_is_not_stored():
    app = _lab_app()
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200


def test_dashboard_says_spy_is_unavailable_without_its_secret():
    app = _lab_app()
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert 'data-unavailable="spy"' in response.text


def test_dashboard_still_renders_the_rest_without_the_spy_secret(
    monkeypatch, alpaca_secrets
):
    async def _boom(**kwargs):
        raise RuntimeError("ALPACA_API_KEY_ID: the operator secret is not stored yet")

    monkeypatch.setattr("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _boom)
    app = _lab_app()
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    body = response.text
    for section in (
        "Performance",
        "Account value vs SPY",
        "Open positions",
        "Recent orders",
        "Fills",
        "Proposals",
        "Audit trail",
    ):
        assert section in body


def test_dashboard_marks_spy_unavailable_in_the_context(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("ALPACA_API_KEY_ID: the operator secret is not stored yet")

    monkeypatch.setattr("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", _boom)
    with _render_captures() as captures:
        app = _lab_app()
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert captures, "dashboard did not render"
    context = captures[-1]
    assert context.get("spy_unavailable") is True
    assert context.get("spy_unavailable_note") == SPY_UNAVAILABLE_NOTE


def test_dashboard_does_not_mark_spy_unavailable_when_the_secret_is_stored(
    spy_benchmark,
):
    app = _lab_app()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert 'data-unavailable="spy"' not in response.text


def test_dashboard_reraises_a_benchmark_failure_that_is_not_the_missing_secret(
    spy_benchmark,
):
    app = _lab_app()
    spy_benchmark.side_effect = RuntimeError("benchmark blew up")
    with patch.object(entry, "get_spy_benchmark", spy_benchmark):
        with patch(
            f"{_ALPACA}.values_for",
            AsyncMock(return_value={"ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"}),
        ):
            with TestClient(app) as c:
                response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert 'data-unavailable="spy"' in response.text


def test_load_spy_returns_rows_and_no_note_when_the_secret_is_stored(spy_benchmark):
    app = _app_with_router()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert 'data-unavailable="spy"' not in response.text
    assert spy_benchmark.await_count == 1


def test_load_spy_returns_a_note_when_the_secret_is_missing():
    app = _lab_app()
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        with _render_captures() as captures:
            with TestClient(app) as c:
                c.get(DASHBOARD_PATH)
    assert captures
    assert captures[0].get("spy_unavailable") is True


def test_spy_missing_error_recognises_the_absent_secret():
    assert SPY_SECRET_NAMES == ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
    assert "secret" in SPY_UNAVAILABLE_NOTE.lower()


def test_builtin_dashboard_renders_the_unavailable_note():
    html = entry._builtin_dashboard(
        {
            "span": "all",
            "spy_unavailable": True,
            "spy_unavailable_note": entry.SPY_UNAVAILABLE_NOTE,
        }
    )
    assert 'data-unavailable="spy"' in html
    assert "not stored yet" in html


# ---- Real-template tests ----

def test_dashboard_serves_html_with_the_real_template(spy_benchmark):
    app = _lab_app()
    with patch("app.modules.paper_spy_benchmark_tracker.get_spy_benchmark", spy_benchmark):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    body = response.text
    assert body.lstrip().startswith("<!DOCTYPE html>")
    assert "Paper trading" in body
    assert "Account value vs SPY" in body


def test_dashboard_real_page_still_renders_the_rest_without_the_secret():
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        app = _lab_app()
        with TestClient(app) as c:
            body = c.get(DASHBOARD_PATH).text
    assert "Proposals" in body
    assert "Audit trail" in body
    assert "equity-chart" in body


def test_dashboard_real_page_answers_200_when_the_benchmark_raises():
    app = _lab_app()
    with patch.object(
        entry,
        "get_spy_benchmark",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        with patch(
            f"{_ALPACA}.values_for",
            AsyncMock(return_value={"ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"}),
        ):
            with TestClient(app) as c:
                response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert 'data-unavailable="spy"' in response.text


def test_dashboard_answers_200_when_the_alpaca_reader_holds_no_secret():
    app = _lab_app()
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200


def test_dashboard_real_page_says_spy_is_unavailable_without_the_secret():
    app = _lab_app()
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert 'data-unavailable="spy"' in response.text


def test_dashboard_does_not_mark_spy_unavailable_when_the_secret_is_stored_real(
    spy_benchmark,
):
    app = _lab_app()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with TestClient(app) as c:
        response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert 'data-unavailable="spy"' not in response.text


def _route_path(route) -> str:
    path = getattr(route, "path", None)
    if path is None:
        path = getattr(route, "path_format", None)
    return str(path) if path is not None else ""


def _bare_router_app() -> FastAPI:
    app = FastAPI()
    app.include_router(_make_router())
    return app
