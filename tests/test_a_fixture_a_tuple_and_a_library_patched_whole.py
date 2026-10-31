"""§17.1882 — a library patched whole through `patch.object` (and what the test sets on it); a fake unpacked from a
helper's tuple; a pytest fixture handed in as a parameter; the last literal bound to a name before a call.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1881 round, 03:54–04:12 UTC; the trace cap at 1 MB let every
attempt's file be replayed): attempts 1, 3 and 4 reached the sandbox at zero findings and failed 6, 23 and 19 tests.
a3: `patch.object(paper_alpaca_paper_client, "httpx") as mock_httpx; mock_httpx.AsyncClient = MagicMock()` (eleven
tests, "'MagicMock' object can't be awaited"), and `client_factory, client_instance, response =
_make_async_client_fake(json_body=…)` with `response = AsyncMock()` inside the helper (eight tests, "'coroutine'
object has no attribute 'get'"). a4: `def patch_alpaca_httpx(): with patch("…httpx.AsyncClient") as m: yield m`, then
`patch_alpaca_httpx.AsyncClient.return_value = fake_client` in the test (two tests awaited the bare mock the real call
returned), and `envelope = {"T": …}` rebound before each of two calls (neither read: a name bound twice was no
literal)."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import unknown_patch_targets, wrong_fakes

ALPACA = '''# app/modules/paper_alpaca_paper_client.py
import httpx

class AlpacaPaperClient:
    def __init__(self) -> None:
        self._client = None

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url="https://paper")
        return self._client

    async def submit_order(self, symbol: str) -> dict:
        client = await self._ensure_client()
        try:
            response = await client.request("POST", "/v2/orders", json={"symbol": symbol})
        except httpx.HTTPError as exc:
            raise RuntimeError(str(exc)) from exc
        return response.json()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
'''

THIRTEEN_F = '''# app/modules/paper_13f_holdings_ingestion.py
async def ingest_13f_holdings(cik: str, *, client) -> int:
    response = await client.get("https://x/" + cik)
    response.raise_for_status()
    data = response.json()
    return len(data.get("filings", {}).get("recent", {}))
'''

TRACKER = '''# app/modules/paper_fill_tracker.py
def is_trade_update(envelope: dict) -> bool:
    return envelope.get("stream") == "trade_updates"
'''

MODULES = {"paper_alpaca_paper_client": ALPACA, "paper_13f_holdings_ingestion": THIRTEEN_F, "paper_fill_tracker": TRACKER}
PATH = "tests/test_paper_twelve_modules.py"


def test_a_library_patched_whole_through_patch_object_and_an_attribute_set_on_it_are_both_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules import paper_alpaca_paper_client

async def test_submit():
    with patch.object(paper_alpaca_paper_client, "httpx") as mock_httpx:
        mock_httpx.AsyncClient = MagicMock()
        client = paper_alpaca_paper_client.AlpacaPaperClient()
        await client.submit_order("SPY")
'''
    assert unknown_patch_targets({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:6` patches `httpx` whole on `paper_alpaca_paper_client`: every class on it "
        "becomes a mock, and the module's `except (httpx.HTTPError)` then fails with `TypeError: catching classes that "
        "do not inherit from BaseException is not allowed` -- patch only the name it leaves the engine through "
        "(`app.modules.paper_alpaca_paper_client.httpx.AsyncClient`)"]
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:6` patches `httpx` on `paper_alpaca_paper_client` and sets `.AsyncClient` to "
        "a fake whose call returns a fake whose `request()` returns a `MagicMock` (line 7), which the module awaits -- a "
        "MagicMock cannot be awaited: make it an `AsyncMock` -- give it what the module uses: "
        "`AsyncClient()->{aclose(), request()->{json()}, is_closed}`"], got
    # a client the module can use: nothing to say about the attribute
    good = test.replace("mock_httpx.AsyncClient = MagicMock()",
                        "fake = MagicMock()\n        fake.request = AsyncMock(return_value=MagicMock())\n"
                        "        fake.aclose = AsyncMock()\n        fake.is_closed = False\n"
                        "        mock_httpx.AsyncClient = MagicMock(return_value=fake)")
    assert wrong_fakes({PATH: good}, MODULES, set()) == []


def test_a_fake_unpacked_from_a_helpers_tuple_is_read_element_by_element():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings

def _make_async_client_fake(*, json_body=None):
    response = AsyncMock()
    response.json.return_value = json_body if json_body is not None else {}
    response.status_code = 200
    client_instance = AsyncMock()
    client_instance.get = AsyncMock(return_value=response)
    client_factory = MagicMock()
    client_factory.return_value = client_instance
    return client_factory, client_instance, response

async def test_it():
    client_factory, client_instance, response = _make_async_client_fake(json_body={"filings": {"recent": []}})
    count = await ingest_13f_holdings("0001", client=client_instance)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:17` hands `ingest_13f_holdings(…)` `client` = a fake whose `get()` returns a "
        "fake whose `json()` returns a coroutine (line 6): an `AsyncMock` method called without `await` (every attribute "
        "of an `AsyncMock` is one), and the module reads it as a dict -- make that object a `MagicMock` (its plain "
        "calls answer at once; give the methods the module awaits an `AsyncMock` each) -- give it what the module uses: "
        "`client{get()->{json(): dict{filings{recent}}, raise_for_status()}}`"], got
    assert wrong_fakes({PATH: test.replace("response = AsyncMock()", "response = MagicMock()")}, MODULES, set()) == []


def test_a_fixture_handed_in_as_a_parameter_is_what_it_yields():
    test = '''# tests/test_paper_twelve_modules.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules import paper_alpaca_paper_client

@pytest.fixture
def patch_alpaca_httpx():
    with patch("app.modules.paper_alpaca_paper_client.httpx.AsyncClient") as m:
        yield m

async def test_submit_order_success(patch_alpaca_httpx):
    patch_alpaca_httpx.AsyncClient.return_value = MagicMock()
    client = paper_alpaca_paper_client.AlpacaPaperClient()
    await client.submit_order("SPY")
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert got == [
        "`tests/test_paper_twelve_modules.py:8` patches `AsyncClient` on `httpx` with a fake whose call returns a fake "
        "whose `request()` returns a `MagicMock` (line 8), which the module awaits -- a MagicMock cannot be awaited: make "
        "it an `AsyncMock` -- give it what the module uses: `AsyncClient()->{aclose(), request()->{json()}, is_closed}`"], got
    # what the test sets on the parameter is set on the patch: a client the module can use
    good = test.replace("patch_alpaca_httpx.AsyncClient.return_value = MagicMock()",
                        "fake = MagicMock()\n    fake.request = AsyncMock(return_value=MagicMock())\n"
                        "    fake.aclose = AsyncMock()\n    fake.is_closed = False\n"
                        "    patch_alpaca_httpx.return_value = fake")
    assert wrong_fakes({PATH: good}, MODULES, set()) == []
    # a fixture no test takes: nothing read in a scope that calls nothing
    unused = test.replace("async def test_submit_order_success(patch_alpaca_httpx):", "async def test_submit_order_success():") \
                 .replace("patch_alpaca_httpx.AsyncClient.return_value = MagicMock()", "pass")
    assert wrong_fakes({PATH: unused}, MODULES, set()) == []


def test_the_last_literal_bound_to_a_name_before_each_call_is_the_one_read():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_fill_tracker

def test_is_trade_update():
    envelope = {"T": "trade_updates"}
    assert paper_fill_tracker.is_trade_update(envelope) is True
    envelope = {"stream": "trade_updates"}
    assert paper_fill_tracker.is_trade_update(envelope) is True
    envelope = {"T": "subscription"}
    assert paper_fill_tracker.is_trade_update(envelope) is False
'''
    defs = es.sibling_definitions([{"node_key": "T18", "title": "tracker", "description": "", "status": "done",
                                    "prompt_template": "Python module `paper_fill_tracker.py`.",
                                    "output_text": f"```python\n{TRACKER}```"}])
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs)]
    assert got == [
        "`tests/test_paper_twelve_modules.py:6` calls `is_trade_update(…)`: `envelope` has none of the keys the function "
        "reads (`stream`): it has `T`",
        "`tests/test_paper_twelve_modules.py:10` calls `is_trade_update(…)`: `envelope` has none of the keys the function "
        "reads (`stream`): it has `T`"]
