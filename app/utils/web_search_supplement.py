"""§17.1438 — an API-keyed search backend behind SearXNG.

SearXNG scrapes public engines, and from this host they refuse it in turn: on 2026-10-09 google cse and
brave were suspended (too many requests), startpage/qwant/duckduckgo CAPTCHA'd, google denied and bing
returned only homepages — every research pass of ADD4's router walkthrough came back with no source.

`SupplementedSearchTransport` wraps the SearXNG client's transport, so every module that searches
(research_agent, execution_agent, assist_research_lib, gt_extractor) gets the same behaviour without a
change at the call site: when a JSON `/search` comes back THIN — fewer than
`web_search_supplement_min_useful` results that are not a site's front page, or an error — the query is
also sent to Ollama's web search API (`OLLAMA_WEB_SEARCH_KEY`) and its results are put first. SearXNG
stays the primary because the API's quota is unpublished. A 401/403/429 cools the API down for
`_COOLDOWN_S`; any other failure returns SearXNG's response unchanged. No key → a pass-through.
"""
from __future__ import annotations

import json
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger("scaffold")

OLLAMA_WEB_SEARCH_URL = "https://ollama.com/api/web_search"
_MAX_RESULTS = 10          # the API's own maximum
_TIMEOUT_S = 20.0
_COOLDOWN_S = 600.0
_cooldown_until = 0.0
_client: httpx.AsyncClient | None = None


def _is_bare_homepage(url: str) -> bool:
    """A site's front door (`https://www.spectrum.net/?msockid=…`) — same rule as research_extractors'."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False
    return bool(u.netloc) and u.path in ("", "/")


def useful_count(results: list) -> int:
    return sum(1 for r in results if isinstance(r, dict) and r.get("url") and not _is_bare_homepage(str(r["url"])))


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT_S)
    return _client


async def ollama_web_search(query: str) -> list[dict]:
    """Results in SearXNG's shape (`title`, `url`, `content`, `engines`), or [] on any failure."""
    global _cooldown_until
    key = settings.ollama_web_search_key
    if not key or not query.strip() or time.monotonic() < _cooldown_until:
        return []
    try:
        resp = await _get_client().post(
            OLLAMA_WEB_SEARCH_URL, json={"query": query, "max_results": _MAX_RESULTS},
            headers={"Authorization": f"Bearer {key}"})
    except Exception as exc:
        logger.warning("web_search_supplement_failed q=%r err=%r", query[:120], exc)
        return []
    if resp.status_code in (401, 403, 429):
        _cooldown_until = time.monotonic() + _COOLDOWN_S
        logger.warning("web_search_supplement_refused status=%d cooldown_s=%d", resp.status_code, int(_COOLDOWN_S))
        return []
    if resp.status_code != 200:
        logger.warning("web_search_supplement_status status=%d q=%r", resp.status_code, query[:120])
        return []
    try:
        rows = resp.json().get("results") or []
    except Exception:
        return []
    return [{"title": str(r.get("title") or ""), "url": str(r["url"]), "content": str(r.get("content") or ""),
             "engine": "ollama", "engines": ["ollama"]}
            for r in rows if isinstance(r, dict) and r.get("url")]


def merge(primary: list[dict], extra: list[dict]) -> list[dict]:
    """`extra` first (it answered when SearXNG did not), then SearXNG's rows it did not repeat."""
    seen = {r["url"] for r in extra}
    return list(extra) + [r for r in primary if not (isinstance(r, dict) and r.get("url") in seen)]


class SupplementedSearchTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self._inner.handle_async_request(request)
        q = request.url.params.get("q") or ""
        if (not settings.ollama_web_search_key or request.url.path != "/search"
                or request.url.params.get("format") != "json" or not q or q == "healthcheck"):
            return resp
        body = await resp.aread()
        data: dict = {}
        if resp.status_code == 200:
            try:
                data = json.loads(body)
            except Exception:
                data = {}
        results = data.get("results") if isinstance(data.get("results"), list) else []
        if resp.status_code == 200 and data and useful_count(results) >= settings.web_search_supplement_min_useful:
            return _rebuilt(resp, request, body)
        extra = await ollama_web_search(q)
        if not extra:
            return _rebuilt(resp, request, body)
        logger.info("web_search_supplemented q=%r searxng_status=%d searxng_useful=%d ollama=%d",
                    q[:120], resp.status_code, useful_count(results), len(extra))
        data = data if isinstance(data, dict) else {}
        data["results"] = merge(results, extra)
        data.setdefault("query", q)
        return httpx.Response(200, json=data, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _rebuilt(resp: httpx.Response, request: httpx.Request, body: bytes) -> httpx.Response:
    """The response with its body already read (and decoded — so no content-encoding)."""
    headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "content-length",
                                                                             "transfer-encoding")]
    return httpx.Response(resp.status_code, headers=headers, content=body, request=request)
