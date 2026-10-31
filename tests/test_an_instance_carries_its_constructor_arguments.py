"""§17.1885 — an instance of a class the test wrote carries its constructor's arguments, a method's `self.x` reads
resolve in that instance's scope, a function's first `return` is the first in source order, and pytest-timeout's own
wording is recognised.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1884 round, 07:50–08:09 UTC; the first run since the keep-list
stopped snowballing -- 18 to 21 tests a draw, down from 267): a4's 13F client was a local class whose `get` returned
`FakeResponse({"filings": {"recent": [ … ]}})` under `if "submissions" in url:` and a bare `FakeResponse(content=…)`
after it; `FakeResponse.__init__` stored `self._data = data` and `json()` returned it. The resolver took the
fallthrough return (breadth-first), then read `self._data` outside the instance -- `json()` "returns None", then
nothing. a2's six "ERROR at setup of … Failed: Timeout (>20.0s) from pytest-timeout" matched no cause: the regex
expected `Timeout >20.0s` without the parentheses."""
from __future__ import annotations

import ast

from app.modules.engine_contracts import _first_return, wrong_fakes
from app.sandbox import engine_tests as et

THIRTEEN_F = '''# app/modules/paper_13f_holdings_ingestion.py
async def ingest_13f_holdings(cik: str, *, client) -> int:
    response = await client.get("https://x/submissions/" + cik)
    submissions = response.json()
    recent = submissions.get("filings", {}).get("recent", {})
    doc = await client.get("https://x/archives/doc.xml")
    return len(recent.get("form", [])) + len(doc.content)
'''

MODULES = {"paper_13f_holdings_ingestion": THIRTEEN_F}
PATH = "tests/test_paper_twelve_modules.py"

TEST = '''# tests/test_paper_twelve_modules.py
import json
from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings

class FakeResponse:
    def __init__(self, data=None, status_code=200, content=None):
        self._data = data
        self.status_code = status_code
        self.content = content if content is not None else b""

    def json(self):
        return self._data

    def raise_for_status(self):
        pass

async def test_ingest_13f_holdings_inserts_rows():
    class Fake13FClient:
        async def get(self, url, **kwargs):
            if "submissions" in url:
                return FakeResponse(RECENT)
            return FakeResponse(content=b"<xml/>")
    count = await ingest_13f_holdings("0001", client=Fake13FClient())
'''


def test_an_instance_carries_its_constructor_arguments_into_its_methods():
    bad = TEST.replace("RECENT", '{"filings": {"recent": [{"form": "13F-HR"}]}}')
    got = wrong_fakes({PATH: bad}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:23` hands `ingest_13f_holdings(…)` `client` = a fake whose `get()` returns a "
        "fake whose `json()` returns a dict[\"filings\"] whose `recent` is a list (line 21), which the module reads as a "
        "dict (`.get(…)` / `[\"key\"]` for `form`)"], got
    good = TEST.replace("RECENT", '{"filings": {"recent": {"form": ["13F-HR"]}}}')
    assert wrong_fakes({PATH: good}, MODULES, set()) == []
    # a keyword constructor argument binds the same way
    kw = TEST.replace("FakeResponse(RECENT)", 'FakeResponse(data={"filings": {"recent": [{"form": "13F-HR"}]}})')
    assert len(wrong_fakes({PATH: kw}, MODULES, set())) == 1


def test_a_functions_first_return_is_the_first_in_source_order():
    fn = ast.parse('''
def get(url):
    if "submissions" in url:
        return 1
    return 2
''').body[0]
    assert ast.unparse(_first_return(fn)) == "1"
    assert _first_return(ast.parse("def f():\n    pass\n").body[0]) is None


def test_pytest_timeouts_own_wording_is_recognised_at_setup_too():
    rx, hint = et.KNOWN_CAUSES[6]
    assert rx.search("E   Failed: Timeout (>20.0s) from pytest-timeout")
    assert rx.search("E   Failed: Timeout >20.0s")
    assert not rx.search("httpx.TimeoutException: timed out")
    assert hint.startswith("a test hung until the sandbox's per-test timeout")
    assert "`ERROR at setup of` means the test never started" in hint
