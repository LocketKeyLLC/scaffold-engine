"""§17.1900 — a `values_for` patch covers only the tests that reach it: the test it is in, a helper the test calls,
a fixture the test takes (an autouse one, every test), module level. A scope is a function at a line, so two tests
of the same name -- a class's and a module-level one, the second a redraw appended -- do not share patches.

Live (follow-up 5b4880c0 T5 a3, 2026-11-03 23:56 UTC): `TestPaperWalkForwardBacktester.test_run_backtest_returns_
expected_keys` ran `run_backtest` with nothing patched -- "Missing Alpaca paper trading secrets" -- while a
module-level test of the same name patched `values_for`; the gate keyed patches by name and saw it covered."""
from __future__ import annotations

from app.modules.engine_contracts import runs_secrets_for_real

PATH = "tests/test_paper_twelve_modules.py"

CLIENT = '''# app/modules/paper_alpaca_paper_client.py
from app.modules.runner_secrets import values_for

async def credentials(db) -> dict:
    return await values_for(db, ["ALPACA_API_KEY_ID"])
'''

BACKTEST = '''# app/modules/paper_walk_forward_backtester.py
from app.modules.paper_alpaca_paper_client import credentials

async def run_backtest(*, db) -> dict:
    return await credentials(db)
'''

MODULES = {"paper_alpaca_paper_client": CLIENT, "paper_walk_forward_backtester": BACKTEST}


def test_a_patch_counts_only_where_the_test_reaches_it():
    test = '''# tests/test_paper_twelve_modules.py
import pytest
from unittest.mock import AsyncMock, patch
from app.modules.paper_walk_forward_backtester import run_backtest

def patch_values_for():
    return patch("app.modules.paper_alpaca_paper_client.values_for", new_callable=AsyncMock, return_value={})

@pytest.fixture
def secrets():
    with patch("app.modules.paper_alpaca_paper_client.values_for", new_callable=AsyncMock, return_value={}):
        yield

class TestBacktest:
    async def test_run(self, db):
        result = await run_backtest(db=db)

async def test_with_helper(db):
    with patch_values_for():
        result = await run_backtest(db=db)

async def test_with_fixture(db, secrets):
    result = await run_backtest(db=db)

async def test_run(db):
    with patch("app.modules.paper_alpaca_paper_client.values_for", new_callable=AsyncMock, return_value={}):
        result = await run_backtest(db=db)
'''
    got = [g.split(" -- the sandbox")[0] for g in runs_secrets_for_real({PATH: test}, MODULES, set())]
    assert got == [
        "`tests/test_paper_twelve_modules.py:16` runs `run_backtest` (`paper_walk_forward_backtester`) for real, and it "
        "reads the operator secret `ALPACA_API_KEY_ID` through `values_for` (`paper_alpaca_paper_client`)"], got
    # an autouse fixture covers every test
    auto = test.replace("@pytest.fixture\ndef secrets", "@pytest.fixture(autouse=True)\ndef secrets")
    assert runs_secrets_for_real({PATH: auto}, MODULES, set()) == []
