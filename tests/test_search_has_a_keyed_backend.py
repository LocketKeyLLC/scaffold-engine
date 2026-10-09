"""§17.1438 — a thin SearXNG answer is supplemented by Ollama's web search API.

2026-10-09: every scraped engine refused this host (google cse / brave suspended, startpage / qwant / ddg
CAPTCHA) and bing answered "Spectrum app port forwarding" with spectrum.com, spectrum.net and
watch.spectrum.net — ten results, none useful, so nothing ever fell back.
"""
import json

import httpx
import pytest

from app.utils import web_search_supplement as wss

HOMEPAGES = {"results": [{"url": "https://www.spectrum.com/?msockid=1", "title": "Spectrum", "content": ""},
                         {"url": "https://www.spectrum.net/?msockid=1", "title": "Spectrum", "content": ""},
                         {"url": "https://watch.spectrum.net/", "title": "Watch", "content": ""}],
             "unresponsive_engines": [["google cse", "Suspended: too many requests"]]}
GOOD = {"results": [{"url": f"https://example.com/p{i}", "title": "t", "content": "c"} for i in range(3)]}
WIKIHOW = {"title": "How to Port Forward on Spectrum", "url": "https://www.wikihow.com/Port-Forward-on-Spectrum",
           "content": "Open the My Spectrum app and tap Services. Tap Router.", "engine": "ollama",
           "engines": ["ollama"]}


def _client(payload, status=200):
    inner = httpx.MockTransport(lambda req: httpx.Response(status, json=payload))
    return httpx.AsyncClient(base_url="http://searxng:8080", transport=wss.SupplementedSearchTransport(inner))


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(wss.settings, "ollama_web_search_key", "k")
    monkeypatch.setattr(wss.settings, "web_search_supplement_min_useful", 3)
    calls = []

    async def fake(q):
        calls.append(q)
        return [WIKIHOW]
    monkeypatch.setattr(wss, "ollama_web_search", fake)
    return calls


@pytest.mark.asyncio
async def test_homepages_only_is_thin_and_is_supplemented(keyed):
    async with _client(HOMEPAGES) as c:
        r = await c.get("/search", params={"q": "Spectrum app port forwarding", "format": "json"})
    data = r.json()
    assert keyed == ["Spectrum app port forwarding"]
    assert data["results"][0]["url"] == WIKIHOW["url"], "the answering backend goes first"
    assert len(data["results"]) == 4 and data["unresponsive_engines"], "SearXNG's rows and health are kept"


@pytest.mark.asyncio
async def test_a_searxng_error_is_supplemented(keyed):
    async with _client({"error": "x"}, status=502) as c:
        r = await c.get("/search", params={"q": "q1 q2", "format": "json"})
    assert r.status_code == 200 and r.json()["results"][0]["url"] == WIKIHOW["url"]


@pytest.mark.asyncio
async def test_a_good_answer_costs_no_api_call(keyed):
    async with _client(GOOD) as c:
        r = await c.get("/search", params={"q": "q", "format": "json"})
    assert keyed == [] and r.json() == GOOD


@pytest.mark.asyncio
async def test_no_key_and_the_health_probe_pass_through(keyed, monkeypatch):
    async with _client(HOMEPAGES) as c:
        r = await c.get("/search", params={"q": "healthcheck", "format": "json"})
        assert keyed == [] and r.json() == HOMEPAGES
        monkeypatch.setattr(wss.settings, "ollama_web_search_key", "")
        r = await c.get("/search", params={"q": "Spectrum", "format": "json"})
    assert keyed == [] and r.json() == HOMEPAGES


@pytest.mark.asyncio
async def test_the_api_is_called_with_the_key_and_cools_down_on_refusal(monkeypatch):
    monkeypatch.setattr(wss.settings, "ollama_web_search_key", "secret-key")
    monkeypatch.setattr(wss, "_cooldown_until", 0.0)
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(200, json={"results": [{"title": "t", "url": "https://a.example/x", "content": "c"}]})
        return httpx.Response(429, json={})
    monkeypatch.setattr(wss, "_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await wss.ollama_web_search("spectrum port forwarding")
    assert out == [{"title": "t", "url": "https://a.example/x", "content": "c", "engine": "ollama", "engines": ["ollama"]}]
    assert str(seen[0].url) == wss.OLLAMA_WEB_SEARCH_URL
    assert seen[0].headers["authorization"] == "Bearer secret-key"
    assert json.loads(seen[0].content) == {"query": "spectrum port forwarding", "max_results": 10}
    assert await wss.ollama_web_search("again") == []          # 429 → cooldown
    assert await wss.ollama_web_search("third") == [] and len(seen) == 2, "no call during the cooldown"


def test_the_searxng_client_carries_the_transport():
    from app.utils import http_clients
    c = http_clients._build_searxng()
    assert isinstance(c._transport, wss.SupplementedSearchTransport)
