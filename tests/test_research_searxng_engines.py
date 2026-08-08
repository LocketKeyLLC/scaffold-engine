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
from app.modules.research_extractors import (
    CATEGORY_ENGINES, _engines_for_category, _GENERAL_BACKBONE,
    SEARXNG_FALLBACK_ENGINES,
)
from app.modules.research_state import ResearchState


@pytest.mark.asyncio
async def test_search_queries_sends_engines_not_categories():
    resp = MagicMock(status_code=200)
    # Non-empty so the §17.712 0-results fallback does NOT fire (single call).
    resp.json = MagicMock(return_value={"results": [
        {"url": "http://x", "title": "t", "content": "c"}]})
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
    # The §17.503 regression: categories must NOT be sent alongside engines.
    assert "categories" not in params
    # §17.712 — the curated (broadened) engines for the category, dynamically.
    assert params["engines"] == _engines_for_category("it")


@pytest.mark.asyncio
async def test_search_queries_zero_results_retries_fallback_engines():
    # §17.712 — a 0-results category query retries ONCE with the widest net;
    # the recovered results are returned.
    empty = MagicMock(status_code=200)
    empty.json = MagicMock(return_value={"results": []})
    recovered = MagicMock(status_code=200)
    recovered.json = MagicMock(return_value={"results": [
        {"url": "http://z", "title": "Zenarmor pricing", "content": "..."}]})
    client = MagicMock()
    client.get = AsyncMock(side_effect=[empty, recovered])

    state = ResearchState(topic="homelab")
    with patch("app.utils.http_clients.get_searxng_client", return_value=client), \
         patch.object(ra, "_searxng_cache_get", AsyncMock(return_value=None)), \
         patch.object(ra, "_searxng_cache_set", AsyncMock()):
        results = await ra._search_queries(
            [{"query": "Zenarmor free vs paid", "facet": "x",
              "search_category": "it"}],
            state,
        )

    assert client.get.await_count == 2                       # category + fallback
    assert client.get.await_args_list[1].kwargs["params"]["engines"] == SEARXNG_FALLBACK_ENGINES
    # the recovered result propagates out (flat list of {title,url,content,facet})
    assert any(r.get("title") == "Zenarmor pricing" for r in results)


class TestEngineMap:
    def test_no_dead_engines_referenced(self):
        # §17.712 — engines that are actually dead on this instance (google is
        # "access denied") must not appear. bing is NO LONGER dead (it is now the
        # most reliable general engine here) — the §17.503 note is stale.
        dead = {"google", "stackoverflow", "pypi", "crossref",
                "semantic_scholar", "google news", "bing news"}
        for cat, engines in CATEGORY_ENGINES.items():
            tokens = {e.strip() for e in engines.split(",")}
            overlap = tokens & dead
            assert not overlap, f"{cat} references dead engine(s): {overlap}"

    def test_every_category_leads_with_general_backbone(self):
        # §17.712 — breadth is resilience: every set includes the general
        # backbone so a single engine's CAPTCHA can't zero the query.
        backbone = set(_GENERAL_BACKBONE.split(","))
        for cat, engines in CATEGORY_ENGINES.items():
            tokens = {e.strip() for e in engines.split(",")}
            assert backbone & tokens, f"{cat} lacks any general-backbone engine"

    def test_default_falls_back_to_general_backbone(self):
        assert _engines_for_category("unknown-cat") == _GENERAL_BACKBONE


# ── §17.729 — relevance filter for keyword-matcher (bing) junk ──────────────

from app.modules.research_extractors import (  # noqa: E402
    relevant_search_results, _query_tokens,
)


def test_relevance_filter_drops_navigational_junk():
    # The live failure shape: bing keyword-matches "install/download" and
    # returns Chrome/Office/banking pages for a technical query.
    q = "install Pi-hole Proxmox LXC container"
    results = [
        {"title": "Pi-hole v6 Installation Guide for Proxmox VE 9 LXC", "content": "pihole lxc"},
        {"title": "Download and install Google Chrome", "content": "browser"},
        {"title": "Current | Future of Banking", "content": "crypto accounts"},
        {"title": "Installing Pi-Hole on Proxmox", "content": "natural born coder"},
    ]
    kept = relevant_search_results(q, results)
    titles = [r["title"] for r in kept]
    assert any("Pi-hole v6" in t for t in titles)
    assert any("Installing Pi-Hole" in t for t in titles)
    assert "Download and install Google Chrome" not in titles  # only filler overlap
    assert "Current | Future of Banking" not in titles


def test_relevance_filter_keeps_all_when_query_is_all_filler():
    # No distinctive tokens to judge on → conservative: keep everything.
    results = [{"title": "anything", "content": "x"}, {"title": "else", "content": "y"}]
    assert relevant_search_results("how do i set it up", results) == results


def test_relevance_filter_preserves_order():
    q = "proxmox zfs pool"
    results = [
        {"title": "zfs pool on proxmox", "content": ""},
        {"title": "unrelated banking", "content": ""},
        {"title": "proxmox storage guide", "content": "zfs"},
    ]
    kept = relevant_search_results(q, results)
    assert [r["title"] for r in kept] == ["zfs pool on proxmox", "proxmox storage guide"]


def test_query_tokens_strips_filler_and_numbers():
    toks = _query_tokens("current Ubuntu Server LTS release version 2026")
    assert "ubuntu" in toks
    assert "lts" in toks
    assert "current" not in toks   # filler
    assert "version" not in toks   # filler
    assert "server" not in toks    # filler
    assert "2026" not in toks      # pure number
