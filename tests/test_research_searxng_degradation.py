"""§17.812 (audit C5) — SearXNG failure handling in _search_queries.

Locks in three behaviors that were silent/poisoning before:
  - a non-200 (CAPTCHA / 429 / 403) flags degradation and does NOT burn the
    query for the session (leaves it retry-able);
  - an empty result list is NOT cached (no cross-session 1h poisoning);
  - a real hit IS cached.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import research_agent as ra
from app.modules.research_state import ResearchState


def _client_returning(status_code, results):
    resp = MagicMock(status_code=status_code)
    resp.json = MagicMock(return_value={"results": results})
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


def _state():
    return ResearchState(topic="x", depth="shallow")


@pytest.fixture(autouse=True)
def _no_politeness_sleep(monkeypatch):
    import app.config
    monkeypatch.setattr(app.config.settings, "research_searxng_delay", 0)


async def _run(client, cset, state, query="homelab zfs pool"):
    with patch("app.utils.http_clients.get_searxng_client", return_value=client), \
            patch.object(ra, "_searxng_cache_get", AsyncMock(return_value=None)), \
            patch.object(ra, "_searxng_cache_set", cset):
        return await ra._search_queries([{"query": query, "facet": "f"}], state)


@pytest.mark.smoke
async def test_non_200_flags_degraded_and_leaves_query_retryable():
    st = _state()
    cset = AsyncMock()
    out = await _run(_client_returning(429, []), cset, st)
    assert out == []
    assert st.search_degraded is True
    assert st.search_history == set(), "non-200 must NOT burn the query (retry-able)"
    cset.assert_not_awaited()


@pytest.mark.smoke
async def test_empty_200_is_not_cached():
    st = _state()
    cset = AsyncMock()
    # 200 with no results; the fallback (same mock) also returns empty.
    out = await _run(_client_returning(200, []), cset, st)
    assert out == []
    cset.assert_not_awaited()  # an empty result must never be cached (poisoning)
    assert st.search_degraded is False  # empty-but-200 is not a backend block


@pytest.mark.smoke
async def test_real_hit_is_cached():
    st = _state()
    cset = AsyncMock()
    # Title shares a distinctive token with the query so the §17.837 relevance
    # gate (default ON since v1.4.0, applied on read after the cache write)
    # keeps the result regardless of the valve.
    hit = [{"title": "Homelab ZFS pool sizing", "url": "https://example.com/a", "content": "c"}]
    out = await _run(_client_returning(200, hit), cset, st)
    assert len(out) == 1 and out[0]["url"] == "https://example.com/a"
    cset.assert_awaited_once()
