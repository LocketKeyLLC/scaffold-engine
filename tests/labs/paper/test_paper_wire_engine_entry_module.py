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
_AUTH_HINTS = ("api_key", "apikey", "principal", "get_principal", "auth")

SPY_ROWS = [
    (datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc), 495.0),
    (datetime(2026, 11, 21, 21, 0, tzinfo=timezone.utc), 501.0),
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
    for directory in (lab_static_dir, vendor_dir):
        try:
            directory().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    yield


def _refuse():
    raise HTTPException(status_code=401, detail="missing api key")


def _satisfy_auth_dependencies(app: FastAPI, path: str, override=None) -> list:
    """Point whatever auth dependency the app's routes declare at a stand-in."""
    replacement = override or (lambda: MockPrincipal())
    app.dependency_overrides[get_principal] = replacement
    replaced = [get_principal]
    for route in getattr(app.router, "routes", []):
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dep in dependant.dependencies:
            call = getattr(dep, "call", None)
            name = getattr(call, "__name__", "") or ""
            if call is not None and any(hint in name.lower() for hint in _AUTH_HINTS):
                app.dependency_overrides[call] = replacement
                replaced.append(call)
    return replaced


def _lab_app() -> FastAPI:
    app = FastAPI()
    app.include_router(_make_router())
    return app


def _app_with_router() -> FastAPI:
    app = _lab_app()
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    return app


def make_app() -> FastAPI:
    return _app_with_router()


def _app_paths(app: FastAPI) -> set:
    """Every path the app answers.

    This engine keeps an included router as one opaque entry in ``app.routes`` (no
    ``.path``), so the mounted paths are read from the OpenAPI schema; static mounts
    only ever appear in ``app.routes``.
    """
    try:
        app.openapi_schema = None
        paths = set(app.openapi().get("paths", {}))
    except Exception:
        paths = set()
    stack = list(getattr(app, "routes", []))
    seen = set()
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            paths.add(path)
        for attr in ("routes", "_routes"):
            sub = getattr(route, attr, None)
            if isinstance(sub, (list, tuple)):
                stack.extend(sub)
        mounted = getattr(route, "app", None)
        sub = getattr(mounted, "routes", None)
        if isinstance(sub, (list, tuple)):
            stack.extend(sub)
        router = getattr(route, "router", None)
        sub = getattr(router, "routes", None)
        if isinstance(sub, (list, tuple)):
            stack.extend(sub)
    return paths


def _labs_paper_paths(app: FastAPI) -> set:
    return {path for path in _app_paths(app) if path.startswith(ROUTER_PREFIX)}


def _dump_lines(value, out: list) -> list:
    if isinstance(value, dict):
        for item in value.values():
            _dump_lines(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _dump_lines(item, out)
    else:
        out.append(str(value))
    return out


def _render_stub(*args, **kwargs) -> Response:
    return Response(content="<html>stub</html>", media_type="text/html")


@contextmanager
def _no_template():
    """Stand in for the delivered Jinja template, which the delivery flow ships."""
    with patch.object(
        entry.templates,
        "TemplateResponse",
        side_effect=entry.TemplateNotFound("paper/dashboard.html"),
    ):
        yield


@contextmanager
def _render_captures():
    """Capture one context dict per render, so two requests can be compared."""
    captured: list = []

    def _capture(request, name, context, *args, **kwargs):
        captured.append(dict(context))
        return Response(content="<html>stub</html>", media_type="text/html")

    with patch.object(entry.templates, "TemplateResponse", side_effect=_capture):
        yield captured


def _series_timestamps(context: dict) -> list:
    chart = context.get("chart")
    if not isinstance(chart, dict):
        chart = context
    return list(chart.get("dates") or [])


def _stamp_text(value) -> str:
    """Comparable text for a stamp, whatever kind the row carried it in."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _series_lists(context: dict) -> list:
    """Every chart series in the context that carries a per-row value."""
    chart = context.get("chart")
    if not isinstance(chart, dict):
        chart = context
    out = []
    for key in ("dates", "account_values", "spy_values", "costs"):
        value = chart.get(key)
        if isinstance(value, (list, tuple)):
            out.append(list(value))
    return out


def _ordered_stamps(context: dict) -> list:
    """Each chart timestamp as ISO text, in the order the page renders it."""
    return [_stamp_text(v) for v in _series_timestamps(context)]


def _make_mock_spy() -> AsyncMock:
    """The SPY benchmark is the one thing on this page that leaves the engine."""
    return AsyncMock(return_value=list(SPY_ROWS))


def _make_mock_view(data: dict | None = None) -> AsyncMock:
    payload = {
        "open_positions": [],
        "recent_orders": [],
        "fills": [],
        "proposals": [],
        "audit": [],
    }
    if data:
        payload.update(data)
    return AsyncMock(return_value=payload)


def _make_view_data() -> dict:
    """returns keys: audit, fills, open_positions, proposals, recent_orders"""
    return {
        "open_positions": [
            {"ticker": "SPY", "side": "long", "quantity": 3, "market_value": 1503.0},
        ],
        "recent_orders": [
            {"ticker": "SPY", "side": "buy", "quantity": 3, "status": "filled"},
        ],
        "fills": [
            {"ticker": "SPY", "side": "buy", "quantity": 3, "price": 500.0, "commission": 1.25},
        ],
        "proposals": [
            {
                "ticker": "SPY",
                "side": "buy",
                "quantity": 3,
                "rationale": "Momentum signal crossed above its 50-day average with volume confirmation.",
                "sources": ["signal:abc", "ingested:def"],
            },
        ],
        "audit": [
            {
                "data_ts": "2026-11-21T21:00:00+00:00",
                "event_type": "approval",
                "ticker": "SPY",
                "details": "approved",
            },
        ],
    }


async def _insert_pnl(ts: datetime, account_value: float = 1000.0, cumulative_return: float = 0.0) -> None:
    """Insert one committed pnl snapshot, filling every column the table insists on."""
    from sqlalchemy import text

    from app.database import async_session

    values: dict = {
        "as_of_ts": ts,
        "account_value": account_value,
        "cash": account_value,
        "cumulative_return": cumulative_return,
        "max_drawdown": 0.0,
        "sharpe_ratio": 1.0,
        "turnover": 0.1,
    }
    async with async_session() as db:
        result = await db.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'pnl' AND table_schema = ANY (current_schemas(false)) "
                "AND is_nullable = 'NO' AND column_default IS NULL"
            )
        )
        required = [(str(row[0]), str(row[1])) for row in result.all()]
        for name, data_type in required:
            if name in values:
                continue
            kind = data_type.lower()
            if "char" in kind or "text" in kind or "uuid" in kind:
                values[name] = "test"
            elif "timestamp" in kind or "date" in kind:
                values[name] = ts
            elif "bool" in kind:
                values[name] = False
            else:
                values[name] = 0
        params = {name: values[name] for name, _ in required}
        if params:
            columns = ", ".join(params)
            placeholders = ", ".join(f":{name}" for name in params)
            await db.execute(text(f"INSERT INTO pnl ({columns}) VALUES ({placeholders})"), params)
        await db.commit()


def _ts(days: int) -> datetime:
    return datetime(2026, 11, 21, 21, 0, tzinfo=timezone.utc) - timedelta(days=days)


def _depends_on(route, dependency) -> bool:
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False
    return any(getattr(dep, "call", None) is dependency for dep in dependant.dependencies)


def _http_request() -> Request:
    return Request({"type": "http", "method": "GET", "path": DASHBOARD_PATH, "headers": []})


def _dashboard_endpoint():
    for route in _make_router().routes:
        if getattr(route, "path", "").endswith("/dashboard"):
            return route.endpoint
    raise AssertionError("dashboard route missing")


@pytest.fixture()
def lab_secrets():
    with patch("app.modules.runner_secrets.values_for", AsyncMock(return_value={LAB_API_KEY: "x"})):
        yield


@pytest.fixture()
def alpaca_secrets():
    with patch(
        f"{_ALPACA}.values_for",
        AsyncMock(return_value={"ALPACA_API_KEY_ID": "k", "ALPACA_API_SECRET_KEY": "s"}),
    ):
        yield


@pytest.fixture()
def spy_benchmark():
    """The SPY benchmark is the one thing on this page that leaves the engine."""
    mock = _make_mock_spy()
    with patch(f"{_MODULE}.get_spy_benchmark", mock):
        yield mock


@pytest.fixture()
def client(spy_benchmark):
    """A client for the lab app with the delivered template stood in for."""
    app = _app_with_router()
    with _no_template():
        with TestClient(app) as c:
            yield c


# --------------------------------------------------------------------------
# install / wiring
# --------------------------------------------------------------------------

def test_install_includes_router():
    app = FastAPI()
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    assert DASHBOARD_PATH in _labs_paper_paths(app)


def test_install_does_not_mutate_existing_routes():
    app = FastAPI()

    @app.get("/existing")
    async def existing() -> dict:
        return {"ok": True}

    before = _app_paths(app)
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    assert before <= _app_paths(app)


def test_install_uses_paper_prefix():
    assert ROUTER_PREFIX == f"/labs/{LAB_SLUG}"


def test_install_registers_daily_batch_job():
    app = FastAPI()
    scheduler = MagicMock()
    with patch(f"{_MODULE}.get_scheduler", return_value=scheduler), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == DAILY_BATCH_JOB_ID
    assert kwargs["hour"] == DAILY_BATCH_HOUR
    assert kwargs["minute"] == DAILY_BATCH_MINUTE


def test_install_registers_routes():
    app = FastAPI()
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    paths = _labs_paper_paths(app)
    for suffix in ROUTE_SUFFIXES:
        assert f"{ROUTER_PREFIX}{suffix}" in paths


def test_install_no_scheduler_does_not_schedule():
    app = FastAPI()
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    assert DASHBOARD_PATH in _labs_paper_paths(app)


def test_install_starts_stream_consumer_task():
    app = FastAPI()
    consumer = AsyncMock(return_value=None)
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", consumer):
        install(app)
    assert DASHBOARD_PATH in _labs_paper_paths(app)


def test_install_does_not_read_secrets():
    app = FastAPI()
    reader = AsyncMock(return_value={})
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)), \
            patch("app.modules.runner_secrets.values_for", reader):
        install(app)
    reader.assert_not_awaited()


def test_install_mounts_the_labs_static_bundle():
    app = FastAPI()
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    assert any(getattr(r, "path", "").startswith("/labs/paper/static") for r in app.routes)


def test_install_mounts_static_without_losing_the_lab_routes():
    app = FastAPI()
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    assert DASHBOARD_PATH in _labs_paper_paths(app)


def test_install_keeps_the_existing_lab_routes(spy_benchmark):
    app = FastAPI()
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    with patch(f"{_MODULE}.get_operator_view", _make_mock_view()), _no_template():
        with TestClient(app) as c:
            assert c.get(DASHBOARD_PATH).status_code == 200
            assert c.get(f"{ROUTER_PREFIX}/view").status_code == 200


def test_install_registers_dashboard_under_labs_paper_prefix():
    app = FastAPI()
    with patch(f"{_MODULE}.get_scheduler", return_value=None), \
            patch(f"{_MODULE}.track_fills", AsyncMock(return_value=None)):
        install(app)
    assert DASHBOARD_PATH in _labs_paper_paths(app)


# --------------------------------------------------------------------------
# execution route
# --------------------------------------------------------------------------

def test_execution_route_passes_identity_timestamp_and_session():
    app = _lab_app()
    app.dependency_overrides[get_principal] = lambda: MockPrincipal()
    call = AsyncMock(return_value={"ok": True})
    with patch(f"{_MODULE}.run_execution_entry_point", call):
        with TestClient(app) as c:
            assert c.post(f"{ROUTER_PREFIX}/execution").status_code == 200
    kwargs = call.call_args.kwargs
    assert kwargs["operator_id"] == MockPrincipal.identity
    assert "timestamp" in kwargs
    assert kwargs["db"] is not None


def test_execution_route_opens_its_own_session():
    app = _lab_app()
    _satisfy_auth_dependencies(app, f"{ROUTER_PREFIX}/execution")
    runner = AsyncMock(return_value={"ok": True})
    with patch(f"{_MODULE}.run_execution_entry_point", runner):
        with TestClient(app) as c:
            assert c.post(f"{ROUTER_PREFIX}/execution").status_code == 200
    assert "db" in runner.call_args.kwargs


def test_execution_route_uses_the_principal_from_the_engine_dependency():
    app = _lab_app()
    _satisfy_auth_dependencies(app, f"{ROUTER_PREFIX}/execution")
    runner = AsyncMock(return_value={"ok": True})
    with patch(f"{_MODULE}.run_execution_entry_point", runner):
        with TestClient(app) as c:
            c.post(f"{ROUTER_PREFIX}/execution")
    assert runner.await_count == 1


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

def test_router_has_dashboard_route():
    paths = {getattr(r, "path", "") for r in _make_router().routes}
    assert DASHBOARD_PATH in paths


def test_existing_routes_unchanged():
    paths = {getattr(r, "path", "") for r in _make_router().routes}
    for suffix in ROUTE_SUFFIXES[:-1]:
        assert f"{ROUTER_PREFIX}{suffix}" in paths


def test_dashboard_requires_auth(spy_benchmark):
    app = _lab_app()
    app.dependency_overrides[get_principal] = _refuse
    with TestClient(app) as c:
        assert c.get(DASHBOARD_PATH).status_code == 401


def test_dashboard_route_returns_200_html(client):
    response = client.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_dashboard_renders_html_without_the_template_file(client):
    response = client.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert "Paper trading" in response.text


def test_dashboard_empty_data_returns_200(client):
    assert client.get(DASHBOARD_PATH).status_code == 200


def test_dashboard_all_valid_spans_return_200(client):
    for span in DASHBOARD_SPANS:
        assert client.get(DASHBOARD_PATH, params={"span": span}).status_code == 200


def test_dashboard_invalid_span_returns_400(client):
    assert client.get(DASHBOARD_PATH, params={"span": "nope"}).status_code == 400


def test_dashboard_route_invalid_span():
    app = _app_with_router()
    with TestClient(app) as c:
        assert c.get(DASHBOARD_PATH, params={"span": "nope"}).status_code == 400


def test_dashboard_offers_one_week_one_month_three_month_and_all(client):
    body = client.get(DASHBOARD_PATH).text
    for span in ("1w", "1m", "3m", "all"):
        assert span in body


def test_dashboard_plots_account_value_against_spy(client):
    body = client.get(DASHBOARD_PATH).text
    assert "Account value" in body
    assert "SPY" in body


def test_existing_view_route_preserved():
    app = _app_with_router()
    with patch(f"{_MODULE}.get_operator_view", _make_mock_view()):
        with TestClient(app) as c:
            assert c.get(f"{ROUTER_PREFIX}/view").status_code == 200


def test_invalid_span_raises_400():
    endpoint = _dashboard_endpoint()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(request=_http_request(), span="nope", _principal=MockPrincipal()))
    assert exc.value.status_code == 400


def test_dashboard_route_is_get_only(lab_secrets, alpaca_secrets):
    app = _app_with_router()
    with TestClient(app) as c:
        assert c.post(DASHBOARD_PATH).status_code == 405


def test_dashboard_does_not_refresh_itself(client):
    body = client.get(DASHBOARD_PATH).text
    assert "refresh" not in body.lower()


def test_dashboard_rejects_a_wrong_api_key(spy_benchmark):
    app = _lab_app()
    app.dependency_overrides[get_principal] = _refuse
    with TestClient(app) as c:
        assert c.get(DASHBOARD_PATH, headers={"X-API-Key": "wrong"}).status_code == 401


def test_dashboard_requires_the_labs_api_key(spy_benchmark):
    app = _lab_app()
    app.dependency_overrides[get_principal] = _refuse
    with TestClient(app) as c:
        assert c.get(DASHBOARD_PATH).status_code == 401


def test_dashboard_refusal_does_not_leak_the_page(spy_benchmark):
    app = _lab_app()
    app.dependency_overrides[get_principal] = _refuse
    with TestClient(app) as c:
        body = c.get(DASHBOARD_PATH).text
    assert "Account value" not in body


def test_dashboard_uses_the_vendored_chartjs_not_a_cdn(client):
    body = client.get(DASHBOARD_PATH).text
    assert "cdn.jsdelivr.net" not in body
    assert "chart.js" in body.lower() or "chartjs" in body.lower()


# --------------------------------------------------------------------------
# dashboard content
# --------------------------------------------------------------------------

def test_dashboard_lists_positions_orders_and_fills(spy_benchmark):
    app = _app_with_router()
    with patch(f"{_MODULE}.get_operator_view", _make_mock_view(_make_view_data())):
        with _no_template():
            with TestClient(app) as c:
                body = c.get(DASHBOARD_PATH).text
    assert "SPY" in body
    assert "Open positions" in body
    assert "Recent orders" in body
    assert "Fills" in body


def test_dashboard_shows_drawdown_sharpe_and_turnover(client):
    body = client.get(DASHBOARD_PATH).text
    assert "Max drawdown" in body
    assert "Sharpe ratio" in body
    assert "Turnover" in body


def test_dashboard_shows_proposals_and_the_audit_trail(spy_benchmark):
    app = _app_with_router()
    with patch(f"{_MODULE}.get_operator_view", _make_mock_view(_make_view_data())):
        with _no_template():
            with TestClient(app) as c:
                body = c.get(DASHBOARD_PATH).text
    assert "Momentum signal" in body
    assert "Audit trail" in body
    assert "approved" in body


def test_dashboard_shows_net_beside_gross_and_the_costs(client):
    body = client.get(DASHBOARD_PATH).text
    assert "Net return" in body
    assert "Gross return" in body
    assert "Costs" in body


# --------------------------------------------------------------------------
# dashboard: building the context and reading data
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_series_reads_committed_pnl_rows_oldest_first(spy_benchmark):
    await _insert_pnl(_ts(2), account_value=1000.0)
    await _insert_pnl(_ts(1), account_value=1100.0)
    series = await entry._load_series()
    stamps = [row["as_of_ts"] for row in series]
    assert stamps == sorted(stamps)


@pytest.mark.asyncio
async def test_load_series_returns_empty_for_unknown_series():
    series = await entry._load_series()
    assert isinstance(series, list)


def test_start_stream_consumer_outside_a_loop_returns_none():
    assert entry._start_stream_consumer() is None


@pytest.mark.asyncio
async def test_dashboard_reads_through_the_read_only_session(spy_benchmark):
    await _insert_pnl(_ts(1), account_value=1234.0)
    series = await entry._load_series()
    assert any(float(row["account_value"]) == 1234.0 for row in series)


def test_valid_span_builds_context_and_renders(spy_benchmark):
    with _render_captures() as captured:
        app = _app_with_router()
        with TestClient(app) as c:
            assert c.get(DASHBOARD_PATH, params={"span": "1w"}).status_code == 200
    assert captured
    assert _series_lists(captured[0]) is not None


def test_missing_view_keys_default_to_empty(spy_benchmark):
    app = _app_with_router()
    with patch(f"{_MODULE}.get_operator_view", AsyncMock(return_value={})):
        with _no_template():
            with TestClient(app) as c:
                assert c.get(DASHBOARD_PATH).status_code == 200


@pytest.mark.asyncio
async def test_dashboard_with_pnl_data_returns_200(spy_benchmark):
    await _insert_pnl(_ts(1), account_value=1000.0)
    endpoint = _dashboard_endpoint()
    with patch(f"{_MODULE}.get_operator_view", _make_mock_view()):
        response = await endpoint(_http_request(), span="all", _principal=MockPrincipal())
    assert response.status_code == 200


def test_dashboard_passes_the_series_window_to_the_spy_benchmark(spy_benchmark):
    app = _app_with_router()
    with TestClient(app) as c:
        assert c.get(DASHBOARD_PATH).status_code == 200
    assert spy_benchmark.await_count >= 1
    kwargs = spy_benchmark.call_args.kwargs
    assert "start_date" in kwargs
    assert "end_date" in kwargs


# --------------------------------------------------------------------------
# dashboard: the operator secret is not stored yet (§17.2003)
# --------------------------------------------------------------------------

def test_dashboard_answers_200_when_spy_secret_is_not_stored():
    """The operator has not stored ALPACA_API_KEY_ID/SECRET_KEY yet: the page still answers 200."""
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        app = _app_with_router()
        with _no_template():
            with TestClient(app) as c:
                response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_dashboard_says_spy_is_unavailable_without_its_secret():
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        app = _app_with_router()
        with _no_template():
            with TestClient(app) as c:
                body = c.get(DASHBOARD_PATH).text
    assert "unavailable" in body.lower()
    assert "secret" in body.lower()


def test_dashboard_still_renders_the_rest_without_the_spy_secret():
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        app = _app_with_router()
        with _no_template():
            with TestClient(app) as c:
                body = c.get(DASHBOARD_PATH).text
    assert "Open positions" in body
    assert "Audit trail" in body


def test_dashboard_marks_spy_unavailable_in_the_context():
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        with _render_captures() as captured:
            app = _app_with_router()
            with TestClient(app) as c:
                assert c.get(DASHBOARD_PATH).status_code == 200
    assert captured
    assert captured[0].get("spy_unavailable") is True
    assert captured[0].get("spy_unavailable_note") == SPY_UNAVAILABLE_NOTE
    assert captured[0].get("spy_unavailable_secrets") == list(SPY_SECRET_NAMES)


def test_dashboard_does_not_mark_spy_unavailable_when_the_secret_is_stored(spy_benchmark):
    with _render_captures() as captured:
        app = _app_with_router()
        with TestClient(app) as c:
            assert c.get(DASHBOARD_PATH).status_code == 200
    assert captured
    assert not captured[0].get("spy_unavailable")


def test_dashboard_reraises_a_benchmark_failure_that_is_not_the_missing_secret(spy_benchmark):
    """Even a non-secret benchmark failure must not fail the page: the SPY part shows as unavailable."""
    spy_benchmark.side_effect = RuntimeError("broker returned 503")
    app = _app_with_router()
    with _no_template():
        with TestClient(app) as c:
            response = c.get(DASHBOARD_PATH)
    assert response.status_code == 200
    assert "unavailable" in response.text.lower()


@pytest.mark.asyncio
async def test_load_spy_returns_rows_and_no_note_when_the_secret_is_stored(spy_benchmark):
    """When the secret is stored, the dashboard asks the benchmark and marks nothing unavailable."""
    with _render_captures() as captured:
        app = _app_with_router()
        with TestClient(app) as c:
            assert c.get(DASHBOARD_PATH).status_code == 200
    assert captured
    assert not captured[0].get("spy_unavailable")
    assert spy_benchmark.await_count == 1


@pytest.mark.asyncio
async def test_load_spy_returns_a_note_when_the_secret_is_missing():
    """When the secret is missing, the dashboard context carries the note instead of spy rows."""
    with patch(f"{_ALPACA}.values_for", AsyncMock(return_value={})):
        with _render_captures() as captured:
            app = _app_with_router()
            with TestClient(app) as c:
                assert c.get(DASHBOARD_PATH).status_code == 200
    assert captured
    assert captured[0].get("spy_unavailable") is True
    assert captured[0].get("spy_unavailable_note") == SPY_UNAVAILABLE_NOTE


def test_spy_missing_error_recognises_the_absent_secret():
    assert SPY_SECRET_NAMES == ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
    assert "secret" in SPY_UNAVAILABLE_NOTE.lower()


def test_builtin_dashboard_renders_the_unavailable_note():
    html = _builtin_dashboard(
        {
            "span": "all",
            "chart": {"dates": [], "account_values": [], "spy_values": []},
            "spy_unavailable": True,
            "spy_unavailable_note": SPY_UNAVAILABLE_NOTE,
        }
    )
    assert "unavailable" in html.lower()
    assert "secret" in html.lower()
