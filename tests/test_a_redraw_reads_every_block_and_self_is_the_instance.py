"""§17.1886 — a redraw answer is read block by block (the last definition of a name wins, prose and snippets skipped,
an unparseable answer costs an ask, not the attempt); `self` in a method is the instance; a constructor's or helper's
argument resolves in the caller's scope; a method's return is read by its callers (through the module's helpers and
an `isinstance` guard); a parameter the function binds itself is not None where it is used; a patch of a name on the
module that defines it is refused when a job module the tested one calls into bound it at import.

Live (follow-up 5b4880c0, 2026-10-31, the §17.1885 round, 08:52–09:24 UTC): T5 a1, a2 and a4 reached the sandbox at
zero findings and failed 8, 5 and 5. a1: `_FakeWebSocket` with no `recv` entered through `__aenter__(self): return
self`; the Alpaca client's `_client` set to an AsyncMock whose response's `json()` was a coroutine, behind
`_ensure_client()` and `return response.json()` -- "Unexpected account response shape"; the backtester reached the
network through the SPY tracker's import-bound `AlpacaPaperClient`. a2: `FakeAsyncClient` with no `get`. a3: the
redraw answer was a draft block, "Wait, …", a snippet and "the final block" -- unparseable as one, None, the attempt
spent with its 18 findings. T5U1 a2/a4: `fetch_disclosures(None)` with `httpx.AsyncClient` patched, refused twice
though the module makes its own client when given None."""
from __future__ import annotations

import asyncio

from app.modules.engine_contracts import (_used_when_none, import_bound_patches, redraw_faked_tests, splice_definitions,
                                          wrong_fakes)
import ast

PATH = "tests/test_paper_twelve_modules.py"

TRACKER = '''# app/modules/paper_fill_tracker.py
import websockets

async def track_fills() -> int:
    async with websockets.connect("wss://x") as ws:
        auth = await ws.recv()
        return len(auth)
'''

ALPACA = '''# app/modules/paper_alpaca_paper_client.py
import httpx

def _parse_account(data: dict) -> str:
    return str(data["id"])

class AlpacaPaperClient:
    def __init__(self) -> None:
        self._client = None

    async def _ensure_client(self):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url="https://paper")
        return self._client

    async def _request(self, method: str, path: str) -> dict:
        client = await self._ensure_client()
        response = await client.request(method, path)
        if response.status_code >= 400:
            raise RuntimeError(response.text[:500])
        return response.json()

    async def get_account(self) -> str:
        data = await self._request("GET", "/v2/account")
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected account response shape")
        return _parse_account(data)
'''

SPY = '''# app/modules/paper_spy_benchmark_tracker.py
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def get_spy_benchmark() -> dict:
    client = AlpacaPaperClient()
    return {"account": await client.get_account()}
'''

BACKTEST = '''# app/modules/paper_walk_forward_backtester.py
from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark

async def run_backtest() -> dict:
    return {"spy": await get_spy_benchmark()}
'''

CONGRESS = '''# app/modules/paper_congressional_trade_ingestion.py
from typing import Optional
import httpx

async def fetch_disclosures(client: Optional[httpx.AsyncClient] = None) -> list:
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()
    response = await client.get("https://x")
    return response.json()

async def count_rows(client) -> int:
    response = await client.get("https://x")
    return len(response.json())
'''

MODULES = {"paper_fill_tracker": TRACKER, "paper_alpaca_paper_client": ALPACA, "paper_spy_benchmark_tracker": SPY,
           "paper_walk_forward_backtester": BACKTEST, "paper_congressional_trade_ingestion": CONGRESS}


def test_a_redraw_answer_is_read_block_by_block_and_the_last_definition_wins():
    code = "import pytest\n\n\ndef test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n"
    answer = ("Here is a draft:\n```python\ndef test_a():\n    assert 10\n```\nWait, that is wrong. A snippet:\n"
              "```python\n    async with x() as y:\n```\nI'll output the final block with all definitions.```python\n"
              "import json\n\ndef test_a():\n    assert 100\n```\n")
    out = splice_definitions(code, answer)
    assert out == "import pytest\nimport json\n\n\ndef test_a():\n    assert 100\n\n\ndef test_b():\n    assert 2\n"
    # a last block cut off without its closing fence still counts
    assert splice_definitions(code, "```python\ndef test_b():\n    assert 20\n") is not None
    assert splice_definitions(code, "Nothing to show.\n```python\n    broken(\n```") is None


def test_an_unparseable_answer_costs_an_ask_not_the_attempt():
    output = "```python\n# tests/test_paper_twelve_modules.py\ndef test_a():\n    assert 1\n\n\ndef test_b():\n    assert 2\n```"
    answers = iter(["Let me think about this first.", "```python\ndef test_a():\n    assert 3\n```"])
    asked: list[list[str] | None] = []

    async def ask(path, refusal, current, only=None):
        asked.append(only)
        return next(answers)

    async def check(candidate):
        return [] if "assert 3" in candidate else ["`tests/test_paper_twelve_modules.py:3` hands `f(…)` `x` = a `MagicMock`"]

    out, left = asyncio.run(redraw_faked_tests(output, ["`tests/test_paper_twelve_modules.py:3` hands `f(…)` `x` = a `MagicMock`"],
                                               ask=ask, check=check, tries=4, tests_only=True))
    assert left == [] and "assert 3" in out and asked == [["test_a"], ["test_a"]]


def test_self_in_a_method_is_the_instance_so_what_it_lacks_is_seen():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import patch
from app.modules import paper_fill_tracker

class _FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

async def test_track_fills():
    fake_ws = _FakeWebSocket(["x"])
    with patch("app.modules.paper_fill_tracker.websockets.connect", return_value=fake_ws):
        await paper_fill_tracker.track_fills()
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:17` patches `connect` on `websockets` with a fake whose call returns a fake "
        "whose `__aenter__()` returns a `_FakeWebSocket` instance (line 5), which has no `recv`, and the module calls "
        "`.recv()` on it"], got
    with_recv = test.replace("    async def __aexit__(self, *exc):", "    async def recv(self):\n        return self.messages.pop(0)\n\n    async def __aexit__(self, *exc):")
    assert wrong_fakes({PATH: with_recv}, MODULES, set()) == []


def test_a_constructors_argument_resolves_in_the_callers_scope():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import patch
from app.modules import paper_fill_tracker

class FakeMessage:
    def __init__(self, text, status):
        self.text = text
        self.status = status

class _FakeWebSocket:
    def __init__(self, text, status=200):
        self._text = text
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        return FakeMessage(self._text, self._status).text

async def test_track_fills():
    with patch("app.modules.paper_fill_tracker.websockets.connect", return_value=_FakeWebSocket("x")):
        await paper_fill_tracker.track_fills()
'''
    assert wrong_fakes({PATH: test}, MODULES, set()) == []     # not "a `FakeMessage` instance, which has no `_status`"


def test_a_methods_return_is_read_by_its_callers_through_the_modules_helpers():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock
from app.modules import paper_alpaca_paper_client

async def test_get_account():
    client = paper_alpaca_paper_client.AlpacaPaperClient()
    mock_http = AsyncMock()
    mock_http.is_closed = False
    client._client = mock_http
    fake_response = AsyncMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"id": "acct123"}
    mock_http.request.return_value = fake_response
    account = await client.get_account()
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- make that")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:9` sets `_client` on a `AlpacaPaperClient` instance to a fake whose "
        "`request()` returns a fake whose `json()` returns a coroutine (line 10): an `AsyncMock` method called without "
        "`await` (every attribute of an `AsyncMock` is one), and the module reads it as a dict"], got
    assert wrong_fakes({PATH: test.replace("fake_response = AsyncMock()", "fake_response = MagicMock()")
                        .replace("import AsyncMock", "import AsyncMock, MagicMock")}, MODULES, set()) == []


def test_a_parameter_the_function_binds_itself_is_not_none_where_it_is_used():
    congress = ast.parse(CONGRESS)
    fns = {f.name: f for f in congress.body if isinstance(f, ast.AsyncFunctionDef)}
    assert _used_when_none(fns["fetch_disclosures"], "client") is False
    assert _used_when_none(fns["count_rows"], "client") is True
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures, count_rows

async def test_fetch():
    fake = MagicMock()
    fake.get = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=[])))
    with patch("app.modules.paper_congressional_trade_ingestion.httpx.AsyncClient", return_value=fake):
        await fetch_disclosures(None)

async def test_count():
    await count_rows(None)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:12` hands `count_rows(…)` `client` = None (line 12), and the module calls `.get()` on it"], got


def test_a_patch_is_refused_when_a_module_the_tested_one_calls_into_bound_the_name_at_import():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_walk_forward_backtester import run_backtest

async def test_backtest():
    with patch("app.modules.paper_alpaca_paper_client.AlpacaPaperClient",
               new=MagicMock(return_value=MagicMock(get_account=AsyncMock(return_value="a")))):
        await run_backtest()
'''
    assert import_bound_patches({PATH: test}, MODULES) == [
        "`tests/test_paper_twelve_modules.py:6` patches `AlpacaPaperClient` on `paper_alpaca_paper_client`, but "
        "`run_backtest` (`paper_walk_forward_backtester`) calls into `paper_spy_benchmark_tracker`, which bound it at "
        "import (`from app.modules.paper_alpaca_paper_client import AlpacaPaperClient`): the patch never reaches it -- "
        "patch `app.modules.paper_spy_benchmark_tracker.AlpacaPaperClient` (where that code looks the name up)"]
    assert import_bound_patches({PATH: test.replace("paper_alpaca_paper_client.AlpacaPaperClient", "paper_spy_benchmark_tracker.AlpacaPaperClient")}, MODULES) == []
