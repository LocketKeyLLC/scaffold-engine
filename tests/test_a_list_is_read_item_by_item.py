"""§17.1880 — an un-awaited AsyncMock method is a coroutine whatever its `return_value`; a list is read item by
item, a patched engine name by the job module that imports it; a key is compared with values; a mock is not a value.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1879 round, 01:44–02:01 UTC): attempts 1 and 4 reached the sandbox at
zero findings and failed 14 and 19 tests. a1: `mock_response = AsyncMock(); mock_response.json.return_value = {…}`
on three congressional tests -- "'coroutine' object has no attribute 'get'" -- the `return_value` chain was followed
before the coroutine check. a4: `patch("app.modules.gt_extractor.search_searxng", return_value={"results": […]})`
where `fetch_news_items` iterates the result and reads `title` from each item (four tests, "'str' object has no
attribute 'get'" -- the patched name lives on an engine module the gate never routed to its user);
`parse_fill_event({"data": {"qty": …, "price": …, "timestamp": …}})` with no `event`, which the tracker compares
with `fill` / `partial_fill` (two); `parse_envelope(MagicMock())` where `raw: bytes | str` goes to `json.loads` (two)."""
from __future__ import annotations

import ast

from app.modules import engine_source as es
from app.modules.engine_contracts import wrong_fakes

CONGRESS = '''# app/modules/paper_congressional_trade_ingestion.py
import httpx

async def _fetch_json(client, url: str) -> dict:
    response = await client.get(url)
    response.raise_for_status()
    return response.json()

def _parse_row(raw: dict) -> dict | None:
    if not raw.get("ticker"):
        return None
    return {"ticker": raw["ticker"], "date": raw.get("transaction_date")}

async def fetch_disclosures(client, *, max_requests: int = 50) -> list:
    data = await _fetch_json(client, "https://x")
    records = data.get("results") or data.get("data") or []
    results: list = []
    results.extend(records)
    rows = []
    for raw in results + []:
        row = _parse_row(raw)
        if row is not None:
            rows.append(row)
    return rows
'''

NEWS = '''# app/modules/paper_news_ingestion.py
async def fetch_news_items(ticker: str, max_results: int = 10) -> list[dict]:
    from app.modules.gt_extractor import search_searxng
    results = await search_searxng(ticker + " news", max_results)
    items = []
    for result in results:
        items.append({"title": result.get("title"), "url": result.get("url")})
    return items
'''

TRACKER = '''# app/modules/paper_fill_tracker.py
import json
from typing import Any

def parse_envelope(raw: bytes | str) -> dict:
    text = raw.decode() if isinstance(raw, bytes) else raw
    return json.loads(text)

def parse_fill_event(envelope: dict[str, Any]) -> dict | None:
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    if event not in ("fill", "partial_fill"):
        return None
    if envelope.get("stream") != "trade_updates":
        return None
    return {"qty": data.get("qty")}
'''

MODULES = {"paper_congressional_trade_ingestion": CONGRESS, "paper_news_ingestion": NEWS, "paper_fill_tracker": TRACKER}
PATH = "tests/test_paper_twelve_modules.py"


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def _calls(test: str) -> list[str]:
    defs = es.sibling_definitions([_row("T2", TRACKER)])
    return [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs, set())]


def test_an_unawaited_asyncmock_method_is_a_coroutine_whatever_its_return_value():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures

async def test_it():
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.json.return_value = {"data": [{"ticker": "AAPL"}]}
    mock_client.get = AsyncMock(return_value=mock_response)
    result = await fetch_disclosures(mock_client, max_requests=1)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:10` hands `fetch_disclosures(…)` `client` = a fake whose `get()` returns a "
        "fake whose `json()` returns a coroutine (line 7): an `AsyncMock` method called without `await` (every attribute "
        "of an `AsyncMock` is one), and the module reads it as a dict -- make that object a `MagicMock` (its plain "
        "calls answer at once; give the methods the module awaits an `AsyncMock` each) -- give it what the module "
        "uses: `client{get()->{json(): dict{data|results[dict{ticker|transaction_date}]}, "
        "raise_for_status()}}`"], got
    # a MagicMock response, its `json` a plain call: read as before
    assert wrong_fakes({PATH: test.replace("mock_response = AsyncMock()", "mock_response = MagicMock()")}, MODULES, set()) == []


def test_a_list_is_read_item_by_item_through_extend_and_concatenation():
    tree = ast.parse(CONGRESS)
    local = es._local_functions(tree)
    assert es._uses_text(es._uses(local["fetch_disclosures"], "client", local)) == \
        "{get()->{json(): dict{data|results[dict{ticker|transaction_date}]}, raise_for_status()}}"
    head = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures

async def test_it():
    response = MagicMock()
    response.json.return_value = PAYLOAD
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    assert len(await fetch_disclosures(client, max_requests=1)) == 1
'''

    def refused(payload: str) -> list[str]:
        return [f.split(" -- give it")[0].split("= a fake", 1)[1] for f in
                wrong_fakes({PATH: head.replace("PAYLOAD", payload)}, MODULES, set())]

    assert refused('{"results": [{"symbol": "AAPL", "amount": 1}]}') == [
        " whose `get()` returns a fake whose `json()` returns a dict[\"results\"][0] with keys `symbol`, `amount` (line 7), "
        "which the module reads for `ticker` / `transaction_date` -- none of them is there"]
    assert refused('{"results": ["AAPL", "MSFT"]}') == [
        " whose `get()` returns a fake whose `json()` returns a list[\"results\"] whose items are strs (line 7), which "
        "the module reads as dicts (`ticker` / `transaction_date`)"]
    assert refused('{"results": [{"ticker": "AAPL"}]}') == []
    assert refused('{"results": []}') == []


def test_a_patched_engine_name_is_checked_by_the_job_module_that_imports_it():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, patch
from app.modules.paper_news_ingestion import fetch_news_items

async def test_it():
    with patch("app.modules.gt_extractor.search_searxng", new_callable=AsyncMock,
               return_value={"results": [{"title": "t", "url": "u"}]}):
        result = await fetch_news_items("SPY", max_results=1)
'''
    assert wrong_fakes({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:6` patches `search_searxng` on `gt_extractor` with a fake whose call "
        "returns a dict (line 7), which the module iterates as a list and reads each item as a dict (`title` / `url`) "
        "-- iterating a dict yields its keys, strings -- give it what the module uses: "
        "`search_searxng()->list[dict{title|url}]`"]
    assert wrong_fakes({PATH: test.replace('{"results": [{"title": "t", "url": "u"}]}', '[{"title": "t", "url": "u"}]')},
                       MODULES, set()) == []
    assert len(wrong_fakes({PATH: test.replace('{"results": [{"title": "t", "url": "u"}]}', '["t", "u"]')},
                           MODULES, set())) == 1
    # an engine name no job module imports: nothing to hold it to
    assert wrong_fakes({PATH: test.replace("gt_extractor.search_searxng", "gt_extractor.other_name")}, MODULES, set()) == []


def test_a_key_compared_with_values_is_shown_and_a_missing_or_wrong_one_is_refused():
    tree = ast.parse(TRACKER)
    local = es._local_functions(tree)
    keys = es._param_keys(local["parse_fill_event"], local)
    assert keys["envelope"]["data"]["sub"]["event"]["values"] == ["fill", "partial_fill"]
    assert keys["envelope"]["stream"]["values"] == ["trade_updates"]
    assert es._keys_text(keys["envelope"]) == "{data{event:fill|partial_fill, qty}, stream:trade_updates}"
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_fill_tracker import parse_fill_event

def test_missing():
    assert parse_fill_event({"stream": "trade_updates", "data": {"qty": "1", "price": "2"}}) is not None

def test_wrong():
    assert parse_fill_event({"stream": "trade_updates", "data": {"event": "new", "qty": "1"}}) is not None

def test_fine():
    assert parse_fill_event({"stream": "trade_updates", "data": {"event": "partial_fill", "qty": "1"}})
'''
    assert _calls(test) == [
        "`tests/test_paper_twelve_modules.py:5` calls `parse_fill_event(…)`: `envelope`[\"data\"] is read for `event` "
        "(one of `fill`, `partial_fill`; without it the function takes the other way) -- the dict given has `qty`, "
        "`price` and no `event`",
        "`tests/test_paper_twelve_modules.py:8` calls `parse_fill_event(…)`: `envelope`[\"data\"][\"event\"] is one of "
        "`fill`, `partial_fill`; it was given 'new'"]


def test_a_mock_for_a_parameter_that_takes_a_value_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import MagicMock
from app.modules.paper_fill_tracker import parse_envelope

def test_it():
    raw = MagicMock()
    raw.decode.return_value = '{"stream": "trade_updates"}'
    result = parse_envelope(raw)
'''
    assert wrong_fakes({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:8` hands `parse_envelope(…)` `raw` = a `MagicMock` (line 6): `raw` is "
        "`bytes | str`, a value the function reads as one (parsed, compared, decoded), never an object with attributes "
        "-- give it what the module uses: a real `bytes | str`"]
    assert wrong_fakes({PATH: test.replace("result = parse_envelope(raw)", "result = parse_envelope(b'{}')")}, MODULES, set()) == []
