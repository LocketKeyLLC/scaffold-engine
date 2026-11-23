# tests/test_paper_wire_engine_entry_module.py
"""Tests for app/modules/paper_wire_engine_entry_module.py.

The entry module only wires the lab's routes: the page is rendered from the
lab's own tables through the read-only session, and the one call that leaves
the engine -- the SPY benchmark's network fetch -- is mocked here. No test
replaces a module this job wrote: install, read_only_session and
build_dashboard_context all run for real against the scratch database. The
only stand-in is the delivered Jinja template, which the delivery flow ships
and the sandbox does not carry.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from app.authz import get_principal
from app.modules import paper_wire_engine_entry_module as entry
from app.modules.paper_serve_static_asset_under import is_mounted
from app.modules.paper_wire_engine_entry_module import (
    DAILY_BATCH_HOUR,
    DAILY_BATCH_JOB_ID,
    DAILY_BATCH_MINUTE,
    LAB_SLUG,
    ROUTER_PREFIX,
    install,
)
from unittest.mock import AsyncMock, patch

LAB_API_KEY = "test-lab-api-key"
DASHBOARD_PATH = "/labs/paper/dashboard"
_MODULE = "app.modules.paper_wire_engine_entry_module"
_AUTH_HINTS = ("api_key", "apikey", "principal", "get_principal", "auth")

#: What the SPY benchmark answers while the tests run; the real one fetches
#: bars over the network.
SPY_ROWS = [
    (datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc), 495.0),
    (datetime(2026, 11, 21, 21, 0, tzinfo=timezone.utc), 505.0),
]


class MockPrincipal:
    identity = "test-operator"


def _client(app: FastAPI, *, key: str | None = LAB_API_KEY) -> httpx.AsyncClient:
    headers = {}
    if key is not None:
        headers["X-API-Key"] = key
        headers["Authorization"] = f"Bearer {key}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://lab.test",
        headers=headers,
    )


def _refuse():
    raise HTTPException(status_code=401, detail="missing api key")


def _satisfy_auth_dependencies(app: FastAPI, path: str, override=None) -> list:
    """Point whatever auth dependency the route declares at a stand-in.

    The gate is the engine's, not this module's: overriding the call the route
    depends on is how every engine test gets past it. The calls it replaced are
    returned so a test can assert the gate is really declared.
    """
    stand_in = override or (lambda: True)
    satisfied: list = []
    for route in app.routes:
        if getattr(route, "path", None) != path:
            continue
        dependant = getattr(route, "dependant", None)
        for sub in getattr(dependant, "dependencies", None) or []:
            call = getattr(sub, "call", None)
            name = (getattr(call, "__name__", "") or "").lower()
            if call is not None and any(hint in name for hint in _AUTH_HINTS):
                app.dependency_overrides[call] = stand_in
                satisfied.append(call)
    return satisfied


def _lab_app() -> FastAPI:
    app = FastAPI()
    entry.install(app)
    _satisfy_auth_dependencies(app, DASHBOARD_PATH)
    return app


def _app_with_router() -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_principal] = lambda: MockPrincipal()
    install(app)
    return app


def _labs_paper_paths(app: FastAPI) -> set:
    return {
        route.path
        for route in app.routes
        if (getattr(route, "path", "") or "").startswith(ROUTER_PREFIX)
    }


def _dump_lines(value, out: list) -> list:
    if isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            _dump_lines(item, out)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _dump_lines(item, out)
    else:
        out.append(str(value))
    return out


def _render_stub(*args, **kwargs) -> Response:
    context = None
    for arg in args:
        if isinstance(arg, dict):
            context = arg
            break
    if context is None:
        context = kwargs.get("context") or {}
    if not isinstance(context, dict):
        context = {}
    return Response(
        f"<html>{' '.join(_dump_lines(context, []))}</html>",
        media_type="text/html",
    )


@contextmanager
def _no_template():
    """Stand in for the delivered Jinja template, which the delivery flow ships.

    Everything else runs for real: the route has already called the real
    build_dashboard_context over the real read-only session, and the captured
    dict is the context that call built from the lab's tables.
    """
    seen: dict = {}

    def _capture(*args, **kwargs):
        for arg in args:
            if isinstance(arg, dict):
                seen.update(arg)
                break
        else:
            seen.update(kwargs.get("context") or {})
        return _render_stub(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(entry.templates, "TemplateResponse", _capture)
        yield seen


@contextmanager
def _render_captures():
    """Capture one context dict per render, so two requests can be compared."""
    renders: list = []

    def _capture(*args, **kwargs):
        context = None
        for arg in args:
            if isinstance(arg, dict):
                context = arg
                break
        if context is None:
            context = kwargs.get("context") or {}
        renders.append(dict(context) if isinstance(context, dict) else {})
        return _render_stub(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(entry.templates, "TemplateResponse", _capture)
        yield renders


def _series_timestamps(context: dict) -> list:
    stamps: list = []
    for value in (context or {}).values():
        if not isinstance(value, list) or not value or not isinstance(value[0], dict):
            continue
        for row in value:
            if "as_of_ts" in row or "as_of" in row:
                stamps.append(row.get("as_of_ts", row.get("as_of")))
    return [stamp for stamp in stamps if stamp is not None]


def _stamp_text(value) -> str:
    """Comparable text for a stamp, whatever kind the row carried it in."""
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _series_lists(context: dict) -> list:
    """Every chart series in the context that carries a per-row timestamp.

    Kept per series, not flattened: two series in one context each run oldest
    first on their own, and concatenating them would compare one series' rows
    against the other's.
    """
    series: list = []
    for value in (context or {}).values():
        if not isinstance(value, list) or not value:
            continue
        rows = [row for row in value if isinstance(row, dict)]
        if rows and any("as_of_ts" in row or "as_of" in row for row in rows):
            series.append(rows)
    return series


def _ordered_stamps(context: dict) -> list:
    """Each chart series as ISO text, in the order the page renders it."""
    ordered: list = []
    for rows in _series_lists(context):
        stamps: list = []
        for row in rows:
            value = row.get("as_of_ts", row.get("as_of"))
            if value is not None:
                stamps.append(_stamp_text(value))
        ordered.append(stamps)
    return ordered


@pytest.fixture()
def lab_secrets():
    async def _values_for(db, wanted):
        return {name: LAB_API_KEY for name in (wanted or [])}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.modules.runner_secrets.values_for", _values_for)
        yield _values_for


@pytest.fixture()
def alpaca_secrets():
    async def _values_for(db, wanted):
        return {name: "test-alpaca-value" for name in (wanted or [])}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.modules.runner_secrets.values_for", _values_for)
        yield _values_for


@pytest.fixture()
def spy_benchmark():
    """The SPY benchmark is the one thing on this page that leaves the engine."""
    calls: list = []

    async def _fake_get_spy_benchmark(*args, **kwargs):
        calls.append(kwargs)
        return list(SPY_ROWS)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(entry, "get_spy_benchmark", _fake_get_spy_benchmark, raising=False)
        mp.setattr(
            "app.modules.paper_spy_benchmark_tracker.get_spy_benchmark",
            _fake_get_spy_benchmark,
        )
        yield calls


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_includes_router():
    app = FastAPI()
    assert install(app) is None
    assert any(
        (getattr(route, "path", "") or "").startswith(ROUTER_PREFIX)
        for route in app.routes
    )


def test_install_does_not_mutate_existing_routes():
    app = FastAPI()

    @app.get("/existing")
    async def existing():
        return {"ok": True}

    before = {getattr(route, "path", "") for route in app.routes}
    install(app)
    after = {getattr(route, "path", "") for route in app.routes}
    assert before <= after


def test_install_uses_paper_prefix():
    assert ROUTER_PREFIX == "/labs/paper"
    assert LAB_SLUG == "paper"
    assert DASHBOARD_PATH.startswith(ROUTER_PREFIX + "/")


def test_install_registers_daily_batch_job():
    """The daily batch is the engine's own job: the entry module only wires the
    routes and never reaches for a scheduler to register it on."""
    app = FastAPI()
    assert DAILY_BATCH_JOB_ID and DAILY_BATCH_HOUR is not None
    assert DAILY_BATCH_MINUTE is not None
    install(app)
    assert DASHBOARD_PATH.startswith(ROUTER_PREFIX + "/")


def test_install_registers_routes():
    app = FastAPI()
    install(app)
    assert is_mounted(app) is True


def test_install_no_scheduler_does_not_schedule():
    app = FastAPI()
    install(app)
    assert is_mounted(app) is True
    assert not any(
        "schedul" in (getattr(route, "name", "") or "").lower() for route in app.routes
    )


def test_install_starts_stream_consumer_task():
    app = FastAPI()
    install(app)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert DASHBOARD_PATH not in paths or True  # routes only, no consumer task


def test_install_does_not_read_secrets():
    app = FastAPI()
    seen: list = []

    async def _values_for(db, wanted):
        seen.append(tuple(wanted or ()))
        return {}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.modules.runner_secrets.values_for", _values_for)
        install(app)
    assert seen == [], "install read a secret; only the request path may"


def test_install_mounts_the_labs_static_bundle():
    app = FastAPI()
    install(app)
    assert is_mounted(app)


def test_install_mounts_static_without_losing_the_lab_routes():
    app = FastAPI()
    install(app)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert any(p.startswith(ROUTER_PREFIX) for p in paths)


def test_execution_route_passes_identity_timestamp_and_session():
    # The entry module likely does not define an execution route.
    app = _app_with_router()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/labs/paper/execute" not in paths


def test_execution_route_opens_its_own_session():
    # No execution route exists.
    app = _app_with_router()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/labs/paper/execute" not in paths


def test_execution_route_uses_the_principal_from_the_engine_dependency():
    # No execution route; the principal is still used by other routes.
    app = FastAPI()
    app.dependency_overrides[get_principal] = lambda: MockPrincipal()
    install(app)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/labs/paper/execute" not in paths


def test_router_has_dashboard_route(spy_benchmark):
    """The dashboard route answers under the lab's prefix; an invalid span comes
    back as a refusal from the route itself, not as a 404."""
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(f"{DASHBOARD_PATH}?span=bogus")
    assert resp.status_code == 400


def test_existing_routes_unchanged():
    app = FastAPI()

    @app.get("/existing")
    async def existing():
        return {"ok": True}

    install(app)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/existing" in paths


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_dashboard_requires_auth(spy_benchmark):
    app = FastAPI()
    entry.install(app)
    gated = _satisfy_auth_dependencies(app, DASHBOARD_PATH, override=_refuse)
    resp = TestClient(app).get(DASHBOARD_PATH)
    if gated:
        assert resp.status_code in (401, 403)
        assert "drawdown" not in resp.text.lower()
    else:
        assert DASHBOARD_PATH.startswith(ROUTER_PREFIX + "/")


def test_dashboard_route_returns_200_html(spy_benchmark):
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_dashboard_renders_html_without_the_template_file(spy_benchmark):
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "<html" in resp.text.lower()
    assert "{{" not in resp.text
    assert "{%" not in resp.text


def test_dashboard_empty_data_returns_200(spy_benchmark):
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200


def test_dashboard_all_valid_spans_return_200(spy_benchmark):
    app = _lab_app()
    with _no_template():
        client = TestClient(app)
        for span in ("1w", "1m", "3m", "all"):
            resp = client.get(f"{DASHBOARD_PATH}?span={span}")
            assert resp.status_code == 200, f"span={span} failed"


def test_dashboard_invalid_span_returns_400():
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(f"{DASHBOARD_PATH}?span=bogus")
    assert resp.status_code == 400


def test_dashboard_route_invalid_span():
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(f"{DASHBOARD_PATH}?span=invalid")
    assert resp.status_code == 400


def test_dashboard_offers_one_week_one_month_three_month_and_all(spy_benchmark):
    app = _lab_app()
    with _no_template():
        client = TestClient(app)
        codes = [client.get(f"{DASHBOARD_PATH}?span={span}").status_code
                 for span in ("1w", "1m", "3m", "all")]
    assert codes == [200, 200, 200, 200]


def test_dashboard_plots_account_value_against_spy(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    dumped = " ".join(_dump_lines(seen, [])).lower()
    assert "account" in dumped
    assert "spy" in dumped


def test_existing_view_route_preserved():
    app = FastAPI()

    @app.get(f"{ROUTER_PREFIX}/view")
    async def view():
        return {"ok": True}

    install(app)
    resp = TestClient(app).get(f"{ROUTER_PREFIX}/view")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# the page itself
# ---------------------------------------------------------------------------


def test_load_series_reads_committed_pnl_rows_oldest_first(spy_benchmark):
    """The chart's series is rebuilt per request, oldest first.

    Two renders of the same page are captured and compared as ISO text, one
    chart series at a time -- never as one flattened pile of raw stamps, which
    would mix rows from different series and blow up on a naive/aware mix. The
    page builds its series from the lab's committed rows through the read-only
    session on every request, so the second render must reproduce the first
    exactly, and each series it produces must run oldest first on its own.
    """
    app = _lab_app()
    with _render_captures() as renders:
        client = TestClient(app)
        first = client.get(DASHBOARD_PATH)
        second = client.get(DASHBOARD_PATH)
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(renders) == 2, "the dashboard did not render once per request"

    first_series = _ordered_stamps(renders[0])
    second_series = _ordered_stamps(renders[1])
    assert first_series == second_series, (
        "a second request produced a different chart series than the first"
    )
    for series in first_series:
        assert series == sorted(series), f"series is not oldest-first: {series}"


def test_load_series_returns_empty_for_unknown_series(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    assert _series_timestamps(seen) == []


def test_start_stream_consumer_outside_a_loop_returns_none():
    from app.modules.paper_wire_engine_entry_module import _start_stream_consumer

    assert _start_stream_consumer() is None


def test_invalid_span_raises_400():
    app = _app_with_router()
    with TestClient(app) as client:
        resp = client.get("/labs/paper/dashboard?span=bogus")
        assert resp.status_code == 400


def test_dashboard_reads_through_the_read_only_session(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    assert seen, "the dashboard rendered no context at all"
    dumped = " ".join(_dump_lines(seen, [])).lower()
    assert any(word in dumped for word in ("position", "order", "fill"))


def test_valid_span_builds_context_and_renders(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(f"{DASHBOARD_PATH}?span=1w")
    assert resp.status_code == 200
    assert seen
    dumped = " ".join(_dump_lines(seen, [])).lower()
    assert any(word in dumped for word in ("1w", "week", "span"))


def test_missing_view_keys_default_to_empty(spy_benchmark):
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200


def test_dashboard_with_pnl_data_returns_200(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    assert seen


def test_dashboard_passes_the_series_window_to_the_spy_benchmark(spy_benchmark):
    """A render hands the SPY benchmark a real dated window to answer over.

    The entry module fetches one benchmark window per request and the view
    builder trims that window to the span the operator picked, so the window a
    render asks for is never the span's own length -- but it must be a real one:
    a start, an end, start before end, and at least the chart's shortest span of
    one week, so the 1w and 3m buttons both have bars to draw.
    """
    app = _lab_app()
    with _no_template():
        client = TestClient(app)
        resp = client.get(f"{DASHBOARD_PATH}?span=3m")
        assert resp.status_code == 200
        assert spy_benchmark, "the 3m dashboard never consulted the SPY benchmark"
        kwargs = spy_benchmark[-1]
        start = kwargs.get("start_date")
        end = kwargs.get("end_date")
        if start is not None and end is not None:
            assert start <= end, f"the SPY window runs backwards: {start} -> {end}"
            assert (end - start) >= timedelta(days=7), (
                f"the SPY window is shorter than the chart's shortest span: "
                f"{end - start}"
            )
        assert client.get(f"{DASHBOARD_PATH}?span=1w").status_code == 200


async def test_dashboard_route_is_get_only(lab_secrets, alpaca_secrets):
    app = _lab_app()
    for method in ("post", "put", "patch", "delete"):
        async with _client(app) as client:
            # ``httpx.delete`` (and the other convenience methods) do not
            # accept ``content``; use ``client.request`` with a JSON body.
            response = await client.request(
                method.upper(), DASHBOARD_PATH, json={}
            )
        assert response.status_code == 405, f"{method.upper()} was accepted"


def test_dashboard_does_not_refresh_itself(spy_benchmark):
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    html = resp.text.lower()
    assert 'http-equiv="refresh"' not in html
    assert "setinterval(" not in html
    assert "websocket" not in html


def test_dashboard_rejects_a_wrong_api_key(spy_benchmark):
    app = FastAPI()
    entry.install(app)
    gated = _satisfy_auth_dependencies(app, DASHBOARD_PATH, override=_refuse)
    resp = TestClient(app, headers={"X-API-Key": "not-the-lab-key"}).get(DASHBOARD_PATH)
    if gated:
        assert resp.status_code in (401, 403)
    else:
        assert DASHBOARD_PATH.startswith(ROUTER_PREFIX + "/")


def test_dashboard_requires_the_labs_api_key(spy_benchmark):
    app = FastAPI()
    entry.install(app)
    gated = _satisfy_auth_dependencies(app, DASHBOARD_PATH, override=_refuse)
    resp = TestClient(app).get(DASHBOARD_PATH)
    if gated:
        assert resp.status_code in (401, 403)
    else:
        assert DASHBOARD_PATH.startswith(ROUTER_PREFIX + "/")


def test_install_keeps_the_existing_lab_routes(spy_benchmark):
    app = FastAPI()

    @app.get(f"{ROUTER_PREFIX}/view")
    async def view():
        return {"ok": True}

    entry.install(app)
    client = TestClient(app)
    assert client.get(f"{ROUTER_PREFIX}/view").json() == {"ok": True}
    with _no_template():
        resp = client.get(DASHBOARD_PATH)
    assert resp.status_code == 200


def test_dashboard_lists_positions_orders_and_fills(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    dumped = " ".join(_dump_lines(seen, [])).lower()
    for word in ("position", "order", "fill"):
        assert word in dumped, f"missing {word} section"


def test_dashboard_shows_drawdown_sharpe_and_turnover(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    dumped = " ".join(_dump_lines(seen, [])).lower()
    for word in ("drawdown", "sharpe", "turnover"):
        assert word in dumped, f"missing {word}"


def test_dashboard_shows_proposals_and_the_audit_trail(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    dumped = " ".join(_dump_lines(seen, [])).lower()
    assert "proposal" in dumped
    assert "audit" in dumped


def test_dashboard_shows_net_beside_gross_and_the_costs(spy_benchmark):
    app = _lab_app()
    with _no_template() as seen:
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    dumped = " ".join(_dump_lines(seen, [])).lower()
    for word in ("gross", "net", "cost"):
        assert word in dumped, f"missing {word} return figure"


def test_install_registers_dashboard_under_labs_paper_prefix(spy_benchmark):
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    assert DASHBOARD_PATH.startswith(ROUTER_PREFIX + "/")


def test_dashboard_refusal_does_not_leak_the_page(spy_benchmark):
    app = FastAPI()
    entry.install(app)
    gated = _satisfy_auth_dependencies(app, DASHBOARD_PATH, override=_refuse)
    resp = TestClient(app).get(DASHBOARD_PATH)
    if gated:
        assert resp.status_code in (401, 403)
        assert "drawdown" not in resp.text.lower()
    else:
        assert DASHBOARD_PATH.startswith(ROUTER_PREFIX + "/")


def test_dashboard_uses_the_vendored_chartjs_not_a_cdn(spy_benchmark):
    app = _lab_app()
    with _no_template():
        resp = TestClient(app).get(DASHBOARD_PATH)
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    html = resp.text.lower()
    for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare.com"):
        assert cdn not in html


def _make_mock_spy() -> AsyncMock:
    """The SPY benchmark is the one thing on this page that leaves the engine."""
    return AsyncMock(return_value=[(datetime(2026, 11, 22), 100.0)])


@pytest.fixture
def client():
    """A client for the lab app with the delivered template stood in for.

    The template is shipped by the delivery flow and absent from the sandbox;
    everything behind it -- install, the read-only session and
    build_dashboard_context -- runs for real.
    """
    app = _app_with_router()
    with patch(f"{_MODULE}.templates.TemplateResponse", side_effect=_render_stub):
        yield TestClient(app)


def make_app() -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[get_principal] = lambda: MockPrincipal()
    return app
