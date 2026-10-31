"""§17.1884 — a session call in a test is awaited; a patch lands where the caller looks the name up; a mock's keyword
is its attribute; a None session handed through a local hop; the keep-list starts over at a reset.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1883 round, 06:42–07:06 UTC): a2 and a3 reached the sandbox at zero
findings and failed 21 and 52. a3: a plain `def make_ingested_item(db, …)` helper did `result = db.execute(…)` and read
`result.scalar_one()` -- "'coroutine' object has no attribute 'scalar_one'", thirty tests. a2: `patch("app.modules.
paper_alpaca_paper_client.AlpacaPaperClient", …)` while the backtester and the SPY tracker, under test, had bound the
class at import -- seven tests reached the network; `MagicMock(json=MagicMock(return_value={"filings": {"recent":
[…]}}))` whose keyword the resolver never read. a1's draw carried 267 tests: the §17.1864 keep-list was the union of
every attempt since the step's text last changed -- across five resets."""
from __future__ import annotations

from app.modules import execution_agent as ea
from app.modules.engine_contracts import import_bound_patches, unawaited_session_calls, wrong_fakes

ALPACA = '''# app/modules/paper_alpaca_paper_client.py
class AlpacaPaperClient:
    async def get_bars(self, symbol: str) -> list:
        return []
'''

SPY = '''# app/modules/paper_spy_benchmark_tracker.py
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def get_spy_benchmark(db) -> dict:
    client = AlpacaPaperClient()
    return {"bars": await client.get_bars("SPY")}
'''

BACKTEST = '''# app/modules/paper_walk_forward_backtester.py
from app.modules import paper_alpaca_paper_client

async def run_backtest(db) -> dict:
    client = paper_alpaca_paper_client.AlpacaPaperClient()
    return {"bars": await client.get_bars("SPY")}
'''

THIRTEEN_F = '''# app/modules/paper_13f_holdings_ingestion.py
async def ingest_13f_holdings(cik: str, *, client) -> int:
    response = await client.get("https://x/" + cik)
    submissions = response.json()
    recent = submissions.get("filings", {}).get("recent", {})
    return len(recent.get("form", []))
'''

EDGAR = '''# app/modules/paper_edgar_form_4_ingestion.py
from sqlalchemy import text

async def ingest_form_4_filings(db, cik: int) -> int:
    await db.execute(text("INSERT INTO ingested_item (source) VALUES ('edgar')"))
    await db.commit()
    return 1

async def ingest_form_4_for_tickers(db, ciks: dict) -> int:
    total = 0
    for cik in ciks.values():
        total += await ingest_form_4_filings(db, cik)
    return total
'''

MODULES = {"paper_alpaca_paper_client": ALPACA, "paper_spy_benchmark_tracker": SPY, "paper_walk_forward_backtester": BACKTEST,
           "paper_13f_holdings_ingestion": THIRTEEN_F, "paper_edgar_form_4_ingestion": EDGAR}
PATH = "tests/test_paper_twelve_modules.py"


def test_a_session_call_that_is_not_awaited_is_refused_and_a_plain_def_helper_is_named():
    test = '''# tests/test_paper_twelve_modules.py
from sqlalchemy import text
from app.database import async_session

def make_ingested_item(db, *, source: str) -> int:
    result = db.execute(text("INSERT INTO ingested_item (source) VALUES (:s) RETURNING id"), {"s": source})
    return result.scalar_one()

async def test_it():
    async with async_session() as session:
        session.commit()
        await session.execute(text("SELECT 1"))
        rows = (await session.execute(text("SELECT 1"))).fetchall()
'''
    assert unawaited_session_calls({PATH: test}) == [
        "`tests/test_paper_twelve_modules.py:6` calls `db.execute(…)` without `await`: the session is an `AsyncSession`, "
        "and `execute` returns a coroutine (`'coroutine' object has no attribute 'scalar_one'`) -- `await db.execute(…)`, "
        "and make `make_ingested_item` an `async def` the test awaits",
        "`tests/test_paper_twelve_modules.py:11` calls `session.commit(…)` without `await`: the session is an "
        "`AsyncSession`, and `commit` returns a coroutine (`'coroutine' object has no attribute 'scalar_one'`) -- "
        "`await session.commit(…)`"]
    assert unawaited_session_calls({"app/modules/x.py": test}) == []


def test_a_patch_on_the_defining_module_is_refused_when_the_caller_bound_the_name_at_import():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch
from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark
from app.modules.paper_walk_forward_backtester import run_backtest

async def test_spy(db):
    with patch("app.modules.paper_alpaca_paper_client.AlpacaPaperClient",
               new=MagicMock(return_value=MagicMock(get_bars=AsyncMock(return_value=[])))):
        await get_spy_benchmark(db)

async def test_backtest(db):
    with patch("app.modules.paper_alpaca_paper_client.AlpacaPaperClient",
               new=MagicMock(return_value=MagicMock(get_bars=AsyncMock(return_value=[])))):
        await run_backtest(db)
'''
    assert import_bound_patches({PATH: test}, MODULES) == [
        "`tests/test_paper_twelve_modules.py:7` patches `AlpacaPaperClient` on `paper_alpaca_paper_client`, but "
        "`get_spy_benchmark` (`paper_spy_benchmark_tracker`) bound it at import (`from app.modules.paper_alpaca_paper_client "
        "import AlpacaPaperClient`): the patch never reaches it -- patch `app.modules.paper_spy_benchmark_tracker.AlpacaPaperClient` "
        "(where that code looks the name up)"]
    # the backtester imports the MODULE and looks the class up at call time: the patch on the source reaches it
    right = test.replace('patch("app.modules.paper_alpaca_paper_client.AlpacaPaperClient",\n               new=MagicMock(return_value=MagicMock(get_bars=AsyncMock(return_value=[])))):\n        await get_spy_benchmark',
                         'patch("app.modules.paper_spy_benchmark_tracker.AlpacaPaperClient",\n               new=MagicMock(return_value=MagicMock(get_bars=AsyncMock(return_value=[])))):\n        await get_spy_benchmark')
    assert import_bound_patches({PATH: right}, MODULES) == []


def test_a_mocks_keyword_is_its_attribute():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings

async def test_it():
    response = MagicMock(json=MagicMock(return_value={"filings": {"recent": [{"form": "13F-HR"}]}}))
    client = MagicMock(get=AsyncMock(return_value=response))
    await ingest_13f_holdings("123", client=client)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:8` hands `ingest_13f_holdings(…)` `client` = a fake whose `get()` returns a "
        "fake whose `json()` returns a dict[\"filings\"] whose `recent` is a list (line 6), which the module reads as a dict "
        "(`.get(…)` / `[\"key\"]` for `form`)"], got
    assert wrong_fakes({PATH: test.replace('"recent": [{"form": "13F-HR"}]', '"recent": {"form": ["13F-HR"]}')}, MODULES, set()) == []


def test_none_for_a_session_handed_through_a_local_hop_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_edgar_form_4_ingestion import ingest_form_4_for_tickers

async def test_it():
    await ingest_form_4_for_tickers(None, {"AAPL": 1})
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:5` hands `ingest_form_4_for_tickers(…)` `db` = None, and the function uses it "
        "(`db{commit(), execute()}`) -- `'NoneType' object has no attribute 'execute'`"], got


def test_the_keep_list_starts_over_at_a_reset():
    assert "e.op IN ('replan', 'restore', 'reset')" in ea._SINCE_THE_STEP_CHANGED_OR_RESET
    assert "e.op IN ('replan', 'restore')" in ea._SINCE_THE_STEP_CHANGED and "'reset'" not in ea._SINCE_THE_STEP_CHANGED
    import inspect
    src = inspect.getsource(ea.prior_tests)
    assert "_SINCE_THE_STEP_CHANGED_OR_RESET" in src
    assert "_SINCE_THE_STEP_CHANGED_OR_RESET" not in inspect.getsource(ea.prior_imports)
