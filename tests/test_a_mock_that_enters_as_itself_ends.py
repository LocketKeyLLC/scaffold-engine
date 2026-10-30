"""§17.1873 — a mock whose `__aenter__` returns itself is resolved to an end, not to a RecursionError.

Live (follow-up 5b4880c0 T5 a1 and a2, 2026-10-30 16:21 and 16:26 UTC): the test file's `_fake_async_client()` set
`client.__aenter__ = AsyncMock(return_value=client)` -- the usual `async with` fake. `_step` resolved that
`return_value` with a fresh hop budget each time, `_resolve` -> `_step` -> `_resolve` never ended, the whole findings
gate died with `RecursionError('maximum recursion depth exceeded')`, the fail-soft logged a warning, and both attempts
went to the sandbox unchecked (where `RiskDecision.passed`, patches of names the modules lack and fake sessions --
every one a §17.1856–1870 refusal -- failed at run time instead)."""
from __future__ import annotations

from app.modules.engine_contracts import wrong_fakes

NEWS = '''# app/modules/paper_news_ingestion.py
import httpx

async def fetch_news_items(ticker: str, client=None) -> list[dict]:
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        async with client as c:
            response = await c.get("https://searxng/search", params={"q": ticker})
            response.raise_for_status()
            return response.json().get("results", [])
    finally:
        if own:
            await client.aclose()
'''

TEST = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_news_ingestion import fetch_news_items

def _fake_httpx_response(json_data=None, status_code=200, text=""):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_data or {}
    response.raise_for_status.return_value = None
    return response

def _fake_async_client(json_data=None):
    response = _fake_httpx_response(json_data=json_data)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client

async def test_news_fetch_items():
    client = _fake_async_client({"results": [{"title": "x"}]})
    items = await fetch_news_items("AAPL", client=client)
    assert items == [{"title": "x"}]
'''


def test_a_self_entering_client_is_resolved_to_an_end():
    found = wrong_fakes({"tests/test_paper_twelve_modules.py": TEST}, {"paper_news_ingestion": NEWS}, set())
    assert found == [], found                    # it does everything the module does with it


def test_a_self_entering_client_whose_get_is_a_magicmock_is_still_refused():
    bad = TEST.replace("client.get = AsyncMock(return_value=response)", "client.get = MagicMock(return_value=response)")
    found = wrong_fakes({"tests/test_paper_twelve_modules.py": bad}, {"paper_news_ingestion": NEWS}, set())
    assert len(found) == 1 and "cannot be awaited" in found[0], found
