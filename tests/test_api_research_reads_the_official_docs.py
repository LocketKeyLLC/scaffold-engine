"""§17.1781 — an API research step reads the API's official documentation first.

Live (job f00ea97f, after §17.1780): API_ALPACA and API_SEC_EDGAR searched, never reached docs.alpaca.markets or the
SEC's fair-access page, and answered "not verified" for the fields the code steps had guessed. Measured from the
engine (2026-10-24): the websocket-streaming page carries `trade_updates`, `execution_id`, `position_qty`; the SEC's
accessing-edgar-data page the User-Agent rule and "10 requests"; the API page `submissions/CIK`.
"""
from __future__ import annotations

import copy
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import engine_plan, execution_evidence as ev
from tests.test_an_engine_plan_is_complete import BRIEF, PLAN_API

PAGE = ("Trade updates. " * 40) + "The trade_updates stream sends fill events with execution_id and position_qty. " * 5


@pytest.mark.asyncio
async def test_official_pages_are_fetched_focused_and_a_shell_is_dropped():
    fetch = AsyncMock(return_value=[{"url": "https://docs.example-api.test/stream", "content": PAGE},
                                    {"url": "https://docs.example-api.test/orders", "content": "<div id=app></div>"}])
    with patch("app.modules.research_agent._fetch_and_extract", new=fetch):
        got = await ev.official_pages(["https://docs.example-api.test/stream", "https://docs.example-api.test/orders"],
                                      "trade_updates fill fields")
    assert [g["url"] for g in got] == ["https://docs.example-api.test/stream"]     # the script-rendered shell is out
    assert got[0]["official"] is True and "execution_id" in got[0]["text"]


@pytest.mark.asyncio
async def test_the_steps_own_pages_come_before_the_search():
    official = AsyncMock(return_value=[{"url": "https://docs.example-api.test/a", "text": "A", "kind": "web"}])
    with patch.object(ev, "official_pages", new=official), \
         patch.object(ev, "finalize_query", return_value="q"), \
         patch.object(ev.settings, "execution_evidence_retrieval_enabled", True), \
         patch("app.modules.assist_research_lib._confirm_query",
               new=AsyncMock(return_value=[{"url": "https://docs.example-api.test/a", "text": "dup"},
                                           {"url": "https://blog.example.test/b", "text": "B"}])), \
         patch.object(ev, "rank_evidence", side_effect=lambda srcs, need, node_key: srcs):
        got = await ev.web_sources(object(), node_key="API_X", domain=None, urls=["https://docs.example-api.test/a"])
    assert [g["url"] for g in got] == ["https://docs.example-api.test/a", "https://blog.example.test/b"]


def test_a_research_step_carries_its_apis_official_pages_and_an_old_one_gains_them():
    tasks = copy.deepcopy(PLAN_API)
    engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert "https://docs.alpaca.markets/docs/websocket-streaming" in by["API_ALPACA"]["tool_config"]["urls"]
    assert "https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data" in by["API_SEC_EDGAR"]["tool_config"]["urls"]
    del by["API_ALPACA"]["tool_config"]                                  # a step added before §17.1781
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    assert changes == ["api_research_docs: API_ALPACA reads 3 official pages"]
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


@pytest.mark.asyncio
async def test_the_executor_hands_a_research_steps_pages_to_the_fetch():
    from tests.test_codegen_for_the_engine_reads_the_engine import _run
    seen = AsyncMock(return_value=[])
    with patch("app.modules.execution_agent.web_sources", new=seen):
        await _run("No code here.", {"description": "Write a news CLI.", "goals": ["A CLI."]},
                   node_extra={"tool": "SearXNG", "tool_config": {"urls": ["https://docs.example-api.test/a"]}})
    assert seen.await_args.kwargs["urls"] == ["https://docs.example-api.test/a"]
