"""§17.837 (plan 8.2 / audit M10) — the §17.729 relevance gate on the primary
research path (`_search_queries`), valve-gated default OFF.

The gate itself (token semantics, filler handling, conservatism) is covered
by the §17.729 tests in test_research_agent_helpers; these tests cover the
NEW wiring: default-off passthrough, fresh-path filtering, cache-hit-path
filtering, and raw-results-cached-before-gating.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.smoke


_ON_TOPIC = {"title": "Redis eviction policies compared",
             "url": "https://example.com/redis", "content": "LRU vs LFU in Redis"}
_JUNK = {"title": "Download Google Chrome",
         "url": "https://example.com/chrome", "content": "Get the new browser"}


def _mock_settings(ms, *, gate: bool):
    ms.research_max_queries = 10
    ms.research_searxng_delay = 0
    ms.research_searxng_concurrency = 3
    ms.research_recency_query_boost = False
    ms.research_relevance_gate_enabled = gate
    ms.research_max_urls_for_depth.return_value = 30


def _searxng_client(results):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": results}
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    return client


async def _run_search(*, gate: bool, cached=None, results=None):
    queries = [{"query": "Redis eviction policy", "facet": "f",
                "search_category": "general"}]
    client = _searxng_client(results or [])
    with patch("app.utils.http_clients.get_searxng_client", return_value=client), \
         patch("app.modules.research_agent._searxng_cache_get",
               AsyncMock(return_value=cached)), \
         patch("app.modules.research_agent._searxng_cache_set",
               AsyncMock()) as cache_set, \
         patch("app.modules.research_agent.settings") as ms, \
         patch("app.modules.research_agent.asyncio.sleep", new_callable=AsyncMock):
        _mock_settings(ms, gate=gate)
        from app.modules.research_agent import ResearchState, _search_queries
        state = ResearchState(topic="t")
        out = await _search_queries(queries, state)
    return out, cache_set


class TestRelevanceGateWiring:
    async def test_default_off_passes_junk_through(self):
        out, _ = await _run_search(gate=False, results=[_ON_TOPIC, _JUNK])
        assert {r["title"] for r in out} == {_ON_TOPIC["title"], _JUNK["title"]}

    async def test_gate_on_drops_junk_fresh_path(self):
        out, _ = await _run_search(gate=True, results=[_ON_TOPIC, _JUNK])
        assert [r["title"] for r in out] == [_ON_TOPIC["title"]]

    async def test_gate_on_drops_junk_cache_hit_path(self):
        out, _ = await _run_search(gate=True, cached=[_ON_TOPIC, _JUNK])
        assert [r["title"] for r in out] == [_ON_TOPIC["title"]]

    async def test_raw_results_cached_before_gating(self):
        """The cache stores RAW results so a valve flip doesn't require a
        cache flush — gating happens on read."""
        _, cache_set = await _run_search(gate=True, results=[_ON_TOPIC, _JUNK])
        cache_set.assert_awaited_once()
        cached_payload = cache_set.await_args.args[1]
        assert len(cached_payload) == 2  # junk included in the stored copy

    async def test_gate_on_keeps_all_on_topic(self):
        second = {"title": "Redis LFU policy tuning",
                  "url": "https://example.com/2", "content": "eviction policy"}
        out, _ = await _run_search(gate=True, results=[_ON_TOPIC, second])
        assert len(out) == 2
