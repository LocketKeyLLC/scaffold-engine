"""§17.503 — SearXNG `engines`-only (no `categories`) regression.

Pre-§17.503 `_search_queries` sent BOTH `categories=<cat>` and a curated
`engines` list. SearXNG treats the two additively: `categories=it`
activates *every* `it`-tagged engine (including MDN, which keyword-matches
aggressively) regardless of the engine list, so a clean homelab query got
flooded with developer.mozilla.org pages. The fix sends `engines` only.

These tests lock in: (1) the request no longer carries `categories`, and
(2) the refreshed engine map never re-references the engines that returned
0 results on this instance (google/bing/stackoverflow/pypi/...).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import research_agent as ra
from app.modules.research_extractors import CATEGORY_ENGINES, _engines_for_category
from app.modules.research_state import ResearchState


@pytest.mark.asyncio
async def test_search_queries_sends_engines_not_categories():
    resp = MagicMock(status_code=200)
    resp.json = MagicMock(return_value={"results": []})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    state = ResearchState(topic="homelab")
    with patch("app.utils.http_clients.get_searxng_client", return_value=client), \
         patch.object(ra, "_searxng_cache_get", AsyncMock(return_value=None)), \
         patch.object(ra, "_searxng_cache_set", AsyncMock()):
        await ra._search_queries(
            [{"query": "homelab proxmox setup", "facet": "x",
              "search_category": "it"}],
            state,
        )

    client.get.assert_called_once()
    args, kwargs = client.get.call_args
    assert args[0] == "/search"
    params = kwargs["params"]
    # The regression: categories must NOT be sent alongside engines.
    assert "categories" not in params
    assert params["engines"] == "github,duckduckgo,startpage"


class TestEngineMap:
    def test_no_dead_engines_referenced(self):
        # These all returned 0 results on this SearXNG instance (google is
        # "Suspended: access denied"); they must not reappear in the map.
        dead = {"google", "bing", "stackoverflow", "pypi", "crossref",
                "semantic_scholar", "google news", "bing news"}
        for cat, engines in CATEGORY_ENGINES.items():
            tokens = {e.strip() for e in engines.split(",")}
            overlap = tokens & dead
            assert not overlap, f"{cat} references dead engine(s): {overlap}"

    def test_default_falls_back_to_reliable_general_web(self):
        assert _engines_for_category("unknown-cat") == "duckduckgo,startpage"
