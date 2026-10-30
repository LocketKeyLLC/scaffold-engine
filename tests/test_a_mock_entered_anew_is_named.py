"""§17.1875 — a mock the module enters with `async with` is a NEW mock unless the test says otherwise; a test that
expects a raise may hand the module no secret; a name imported inside a function lives on its own module.

Live (follow-up 5b4880c0 T5, 2026-10-30 18:31–18:51 UTC, the §17.1874 round): four attempts, eight redraws, none
reached the sandbox. Left every time: `patch("app.modules.paper_news_ingestion.httpx…")` -- the news ingester reaches
SearXNG through `from app.modules.gt_extractor import search_searxng` INSIDE `fetch_news_items`, and nothing told the
test so; `fake_client.get = AsyncMock(…)` patched as `httpx.AsyncClient`'s instance, which the module enters with
`async with` -- measured: `async with MagicMock() as c` yields a fresh AsyncMock, the client's `get` never reached, and
the finding asked for a MagicMock response the test already had; and `values_for` patched to return `{}` under
`pytest.raises(AlpacaSecretsMissingError)`, refused for returning no key."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import unknown_patch_targets, wrong_fakes, wrong_secret_mocks

REDDIT = '''# app/modules/paper_reddit_ingestion.py
import httpx

async def fetch_posts(url: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json().get("data", {}).get("children", [])
'''


def _fakes(test: str) -> list[str]:
    return wrong_fakes({"tests/test_paper_twelve_modules.py": test}, {"paper_reddit_ingestion": REDDIT}, set())


def test_a_client_the_module_enters_is_named_as_entered_anew():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_reddit_ingestion import fetch_posts

async def test_it():
    fake_response = MagicMock()
    fake_response.json.return_value = {"data": {"children": []}}
    fake_response.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.aclose = AsyncMock()
    with patch("app.modules.paper_reddit_ingestion.httpx.AsyncClient", MagicMock(return_value=fake_client)):
        assert await fetch_posts("u") == []
'''
    found = _fakes(test)
    assert len(found) == 1, found
    assert found[0].startswith("`tests/test_paper_twelve_modules.py:12` patches `AsyncClient` on `httpx` with a fake whose "
                               "call returns a fake whose `__aenter__()` returns a NEW AsyncMock"), found[0]
    assert "(`aclose, get`) is never reached" in found[0] and "`fake.__aenter__ = AsyncMock(return_value=fake)`" in found[0]
    # the test that says so passes
    entered = test.replace("    fake_client.aclose = AsyncMock()\n",
                           "    fake_client.aclose = AsyncMock()\n    fake_client.__aenter__ = AsyncMock(return_value=fake_client)\n")
    assert _fakes(entered) == []
    # a bare client the test set nothing on is still read as before (the module gets a fresh AsyncMock)
    bare = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_reddit_ingestion import fetch_posts

async def test_it():
    with patch("app.modules.paper_reddit_ingestion.httpx.AsyncClient", MagicMock(return_value=MagicMock())):
        await fetch_posts("u")
'''
    bare_found = _fakes(bare)
    assert len(bare_found) == 1 and "NEW AsyncMock" not in bare_found[0], bare_found


CLIENT = '''# app/modules/paper_alpaca_paper_client.py
from app.modules.runner_secrets import values_for

ALPACA_API_KEY_SECRET_NAME = "ALPACA_API_KEY_ID"

class AlpacaSecretsMissingError(RuntimeError):
    pass

async def _get_credentials(db) -> dict:
    secrets = await values_for(db, [ALPACA_API_KEY_SECRET_NAME])
    if not secrets.get(ALPACA_API_KEY_SECRET_NAME):
        raise AlpacaSecretsMissingError("missing")
    return secrets
'''


def test_a_secret_mock_under_an_expected_raise_is_not_refused():
    raising = '''# tests/test_paper_twelve_modules.py
import pytest
from unittest.mock import AsyncMock, patch
from app.modules.paper_alpaca_paper_client import AlpacaSecretsMissingError, _get_credentials

async def test_missing_secrets_raises(db):
    with patch("app.modules.paper_alpaca_paper_client.values_for", AsyncMock(return_value={})):
        with pytest.raises(AlpacaSecretsMissingError):
            await _get_credentials(db)
'''
    files = {"tests/test_paper_twelve_modules.py": raising}
    assert wrong_secret_mocks(files, {"paper_alpaca_paper_client": CLIENT}, set()) == []
    plain = raising.replace("        with pytest.raises(AlpacaSecretsMissingError):\n            await _get_credentials(db)\n",
                            "        await _get_credentials(db)\n")
    found = wrong_secret_mocks({"tests/test_paper_twelve_modules.py": plain}, {"paper_alpaca_paper_client": CLIENT}, set())
    assert len(found) == 1 and "to return no key" in found[0], found


NEWS = '''# app/modules/paper_news_ingestion.py
import logging
from typing import Any

logger = logging.getLogger("scaffold")

async def fetch_news_items(query: str, max_results: int = 20) -> list[dict[str, Any]]:
    from app.modules.gt_extractor import search_searxng

    results = await search_searxng(query, max_results)
    return [r for r in results if r.get("title")]
'''


def test_a_name_imported_at_call_time_is_named_in_the_finding_and_the_index():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_news_ingestion import fetch_news_items

async def test_news():
    with patch("app.modules.paper_news_ingestion.httpx.AsyncClient", MagicMock()):
        assert await fetch_news_items("AAPL") == []
'''
    found = unknown_patch_targets({"tests/test_paper_twelve_modules.py": test}, {"paper_news_ingestion": NEWS}, set())
    assert len(found) == 1, found
    assert ("It reaches outside through `fetch_news_items`, which imports `app.modules.gt_extractor.search_searxng` at "
            "call time -- patch THOSE names on their own modules") in found[0], found[0]
    # the index says so too, both renderings
    defs = es.sibling_definitions([{"node_key": "T3", "status": "done", "output_text": "```python\n" + NEWS + "```"}])
    assert defs["paper_news_ingestion"]["call_imports"] == {"fetch_news_items": ["app.modules.gt_extractor.search_searxng"]}
    assert "[imports at call time: app.modules.gt_extractor.search_searxng]" in es._public_api(NEWS)
    assert "[imports at call time: app.modules.gt_extractor.search_searxng]" in es._render_job_modules(defs, "paper_tests")
    # a name imported inside the function and patched on the module: the finding says where it lives
    lazy = NEWS.replace("    from app.modules.gt_extractor import search_searxng\n\n    results = await search_searxng(query, max_results)\n",
                        "    import httpx\n\n    async with httpx.AsyncClient() as c:\n        results = (await c.get(query)).json()\n")
    found = unknown_patch_targets({"tests/test_paper_twelve_modules.py": test}, {"paper_news_ingestion": lazy}, set())
    assert len(found) == 1 and ("`httpx` is imported inside `fetch_news_items` at call time and is never an attribute of "
                                "`paper_news_ingestion`: patch the name on `httpx` itself") in found[0], found
