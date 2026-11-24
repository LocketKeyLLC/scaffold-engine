"""§17.2005 — a check that stands after the job's answered delivery question is handed the lab's pages as the running
engine serves them: each GET view of the job's entry, with and without the API key, the assets it loads, the
same-page links it offers, its data arrays and its visible text.

Live (69fe61c4 T13 "Verify dashboard renders", 2026-11-24): the check -- an LLM step -- refused three times, "this
step requires live verification against a running engine … I cannot run the checks and will not fabricate results",
and the job blocked. The fixtures are made up in the live shape (a dashboard page with span links, a vendored chart
script, a JSON view)."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.modules import lab_pages as lp

ENTRY = '''from fastapi import APIRouter


def _make_router():
    router = APIRouter(prefix="/labs/lab")

    @router.get("/dashboard")
    async def dashboard(span: str = "all"):
        return {}

    @router.get("/view")
    async def view():
        return {}

    @router.get("/item/{item_id}")
    async def item(item_id: str):
        return {}

    @router.post("/run")
    async def run():
        return {}

    return router
'''


def _app() -> FastAPI:
    app = FastAPI()
    r = APIRouter(prefix="/labs/lab")

    def _auth(key):
        if key != "k":
            raise HTTPException(401)

    @r.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(span: str = "all", x_api_key: str | None = Header(None)):
        _auth(x_api_key)
        n = {"1w": 1, "all": 3}.get(span, 0)
        data = json.dumps({"dates": list(range(n)), "account_values": [1.0] * n})
        return (f"<html><head><title>Lab &mdash; dashboard</title><script src=\"/labs/lab/static/chart.js\"></script>"
                f"</head><body><a href=\"?span=1w\">1w</a><a href=\"?span=all\">all</a><p>Net return +1.00%</p>"
                f"<script>const chartData = {data};</script></body></html>")

    @r.get("/view")
    async def view(x_api_key: str | None = Header(None)):
        _auth(x_api_key)
        return {"positions": []}

    @r.get("/item/{item_id}")
    async def item(item_id: str):
        return {}

    @r.get("/static/chart.js", response_class=PlainTextResponse)
    async def chart():
        return "x" * 100

    app.include_router(r)
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://engine")


async def test_the_lab_pages_are_read_with_and_without_the_key_with_their_assets_links_and_data():
    out = await lp.read_lab_pages(ENTRY, "http://engine", "k", client=_client())
    assert "## The running engine's lab pages, read by the engine just now (§17.2005)" in out
    assert "### GET /labs/lab/dashboard\n- with the API key: 200 text/html" in out and "without it: 401" in out
    assert "- title: Lab — dashboard" in out
    assert "- loads `/labs/lab/static/chart.js`: 200, 100 bytes" in out
    assert '- link `?span=1w`: 200' in out and '(differs); arrays {"dates": 1, "account_values": 1}' in out
    assert "(same page as the default)" in out                                       # ?span=all
    assert '- data arrays in the page (name: items): {"dates": 3, "account_values": 3}' in out
    assert "- visible text: Lab — dashboard 1w all Net return +1.00%" in out
    assert '### GET /labs/lab/view\n- with the API key: 200 application/json' in out and '{"positions":[]}' in out
    assert "/item/" not in out and "/run" not in out                                 # a path parameter; a POST


async def test_a_page_the_engine_cannot_read_is_said_never_invented():
    def boom(_request):
        raise httpx.ConnectError("refused")
    c = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://engine")
    out = await lp.read_lab_pages(ENTRY, "http://engine", "k", client=c)
    assert "- the engine could not read its own pages: ConnectError: refused" in out


def test_the_paths_are_the_entrys_get_views_as_the_engine_serves_them():
    served = ["/labs/lab/dashboard", "/labs/lab/view", "/labs/lab/item/{item_id}", "/labs/other/thing", "/health"]
    assert lp.lab_paths(ENTRY, served) == ["/labs/lab/dashboard", "/labs/lab/view"]


def _nodes(t12_status="done"):
    return [
        {"node_key": "T8", "title": "Add dashboard route", "status": "done", "depends_on": []},
        {"node_key": "T12", "title": "Is this job's code merged into the engine and running?", "status": t12_status,
         "depends_on": ["T8"]},
        {"node_key": "T13", "title": "Verify dashboard renders", "status": "running", "depends_on": ["T12"]},
        {"node_key": "T16", "title": "Document the dashboard", "status": "pending", "depends_on": ["T13"]},
        {"node_key": "T11", "title": "Verify existing routes unchanged", "status": "done", "depends_on": ["T8"]},
    ]


@pytest.mark.parametrize("key,status,want", [("T13", "done", True), ("T16", "done", True),
                                             ("T11", "done", False), ("T13", "pending", False)])
def test_a_step_is_after_the_delivery_only_once_the_question_is_answered(key, status, want):
    assert lp.after_delivery(_nodes(status), key) is want


def test_only_a_check_is_given_the_pages():
    assert lp.is_check({"node_type": "checkpoint", "title": "Docs"})
    assert lp.is_check({"node_type": "task", "title": "Verify dashboard renders"})
    assert not lp.is_check({"node_type": "task", "title": "Document the dashboard"})


async def test_block_for_step_gives_nothing_to_a_code_step_or_a_non_check_before_reading_anything():
    class NoDb:
        async def execute(self, *a, **k):
            raise AssertionError("not read")
    assert await lp.block_for_step(NoDb(), "j", {"node_key": "T8", "title": "Verify x", "tool": "CodeGen"}) == ""
    assert await lp.block_for_step(NoDb(), "j", {"node_key": "T16", "title": "Document", "tool": "LLM"}) == ""
