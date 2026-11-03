"""§17.1894 — a call to a helper the test file itself defines is checked against the definition: a keyword it has
no parameter for, a required argument left out, too many positional ones, `await` on a plain `def`, a bare call on
an `async def` whose coroutine is then used. Fixtures are left alone (pytest hands them in).

Live (follow-up 5b4880c0 T5 a3, 2026-11-03 21:50 UTC): `_insert_ingested_item(db, source="sec_edgar", ticker=ticker)`
inside `_create_chain`, which six tests call -- "TypeError: _insert_ingested_item() got an unexpected keyword
argument 'ticker'", six of six sandbox failures from one line, and the job functions' signatures had been checked
since §17.1756."""
from __future__ import annotations

from app.modules.engine_contracts import own_helper_calls

PATH = "tests/test_paper_twelve_modules.py"


def test_a_helpers_signature_and_its_await_are_held_to_and_a_fixture_is_not():
    test = '''# tests/test_paper_twelve_modules.py
import pytest
from datetime import datetime

async def _insert_ingested_item(db, *, source: str = "sec_edgar", external_id: str | None = None) -> int:
    return 1

def _envelope(stream: str, data: dict) -> dict:
    return {"stream": stream, "data": data}

@pytest.fixture
def db_session():
    yield None

async def _create_chain(db, ticker: str):
    item_id = await _insert_ingested_item(db, source="sec_edgar", ticker=ticker)
    other = await _insert_ingested_item(db, "sec_edgar")
    env = _envelope("trade_updates")
    env2 = await _envelope("trade_updates", {})
    pending = _insert_ingested_item(db)
    fine = await _insert_ingested_item(db, external_id="x")
    return item_id

async def test_uses_fixture(db_session):
    chain = await _create_chain(db_session, "SPY")
    assert db_session(1) is None
'''
    got = own_helper_calls({PATH: test})
    tail = " -- `{h}` is this file's own (line {ln}): call it as it is defined, or change the definition"
    assert got == [
        "`tests/test_paper_twelve_modules.py:16` calls `_insert_ingested_item(…)` has no parameter `ticker` (it takes "
        "db, *, source, external_id)" + tail.format(h="_insert_ingested_item", ln=5),
        "`tests/test_paper_twelve_modules.py:17` calls `_insert_ingested_item(…)` takes at most 1 positional argument(s) "
        "(db); it was given 2" + tail.format(h="_insert_ingested_item", ln=5),
        "`tests/test_paper_twelve_modules.py:18` calls `_envelope(…)` is missing required argument(s) `data`"
        + tail.format(h="_envelope", ln=8),
        "`tests/test_paper_twelve_modules.py:19` calls `_envelope(…)` under `await`: a plain `def`, whose result cannot "
        "be awaited -- drop the `await`" + tail.format(h="_envelope", ln=8),
        "`tests/test_paper_twelve_modules.py:20` calls `_insert_ingested_item(…)` without `await`: an `async def`, so "
        "what it holds is a coroutine, never the result -- `await _insert_ingested_item(…)`"
        + tail.format(h="_insert_ingested_item", ln=5)], got
    assert own_helper_calls({"app/modules/x.py": test}) == []
    assert own_helper_calls({PATH: "def test_a():\n    assert True\n"}) == []
