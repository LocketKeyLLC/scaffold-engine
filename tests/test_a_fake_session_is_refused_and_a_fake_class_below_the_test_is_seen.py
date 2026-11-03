"""§17.1892 — what the first run on §17.1891 showed: a fake class defined BELOW the test was never resolved (its
plain `get` awaited by the module, unrefused); a fake session (`class FakeDB` with an `execute` that answers canned
results) handed to a function that runs SQL on it; an empty list the function fills (`approvals: list = []`, then
`.append`) compared with a number; `-rA`'s PASSES section (captured logs of passing tests) shown to the model; a
`Decimal` mixed with a float inside a module (a CALLEE cause: the module's, whatever the test fed); and the kept-tests
splice that sat after the draw's early return and never ran.

Live (follow-up 5b4880c0 T5, 2026-11-03 21:01–21:20 UTC): a2 reached the sandbox at zero findings and failed 9 of
17 -- `'_FakeSecClient' object can't be awaited`, `'FakeResult' object has no attribute 'scalar_one'`, `'dict' object
has no attribute '_mapping'`, `assert [{...}] == 1`, `unsupported operand type(s) for -: 'decimal.Decimal' and
'float'`; a3 drew every body anew, its 8 kept tests unspliced (no `kept_passing_tests_spliced` in the log)."""
from __future__ import annotations

import inspect

from app.modules import execution_agent as ea
from app.modules.engine_contracts import fake_sessions, returned_compares, wrong_fakes
from app.sandbox import engine_tests as et

PATH = "tests/test_paper_twelve_modules.py"

EDGAR = '''# app/modules/paper_edgar_form_4_ingestion.py
async def _fetch_submissions(client, cik: int) -> dict:
    response = await client.get("https://x/" + str(cik))
    response.raise_for_status()
    return response.json()

async def ingest_form_4_filings(db, cik: int, *, client) -> int:
    submissions = await _fetch_submissions(client, cik)
    return len(submissions.get("recent", {}).get("form", []))
'''

EQUITY = '''# app/modules/paper_day_start_equity_recording.py
from sqlalchemy import text

async def record_day_start_equity(*, db, equity) -> int:
    result = await db.execute(text("INSERT INTO equity (value) VALUES (:v) RETURNING id"), {"v": equity})
    await db.commit()
    return int(result.scalar_one())

async def _read(session) -> list:
    return (await session.execute(text("SELECT 1"))).fetchall()

async def get_day_start_equity(db) -> list:
    return await _read(db)

def describe(db) -> str:
    return "a session"
'''

ENTRY = '''# app/modules/paper_execution_entry_point.py
async def run_execution_entry_point(*, db, operator_id: str) -> dict:
    approvals: list[dict] = []
    orders = []
    for p in await _pending(db):
        approvals.append({"proposal_id": p})
    return {"approvals": approvals, "orders": orders, "count": len(approvals)}

async def _pending(db) -> list:
    return []
'''

MODULES = {"paper_edgar_form_4_ingestion": EDGAR, "paper_day_start_equity_recording": EQUITY,
           "paper_execution_entry_point": ENTRY}


def test_a_fake_class_defined_below_the_test_is_the_one_it_builds():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_edgar_form_4_ingestion

async def test_ingest_form_4_filings(db):
    fake_client = _FakeSecClient({"recent": {"form": ["4"]}})
    count = await paper_edgar_form_4_ingestion.ingest_form_4_filings(db, cik=123, client=fake_client)

class _FakeSecResponse:
    def __init__(self, data: dict):
        self._data = data

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        pass

class _FakeSecClient:
    def __init__(self, data: dict):
        self._data = data

    def get(self, url: str, **kwargs):
        return _FakeSecResponse(self._data)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:6` hands `ingest_form_4_filings(…)` `client` = a fake whose `get` is a "
        "plain `def` (line 22), whose result the module awaits -- make it `async def` (or an `AsyncMock(return_value=…)`)"], got
    assert wrong_fakes({PATH: test.replace("    def get(self, url: str, **kwargs):", "    async def get(self, url: str, **kwargs):")},
                       MODULES, set()) == []


def test_a_fake_session_handed_to_a_function_that_runs_sql_on_it_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from decimal import Decimal
from app.database import async_session
from app.modules import paper_day_start_equity_recording
from app.modules.paper_day_start_equity_recording import describe, get_day_start_equity

class FakeResult:
    def __init__(self, rows=None, scalar_value=None):
        self._rows, self._scalar = rows or [], scalar_value

    def fetchall(self):
        return self._rows

class FakeDB:
    def __init__(self, results=None):
        self._results = list(results or [])

    async def execute(self, query, params=None):
        return self._results.pop(0)

    async def commit(self):
        pass

async def test_record():
    fake_db = FakeDB(results=[FakeResult(scalar_value=1)])
    recorded_id = await paper_day_start_equity_recording.record_day_start_equity(db=fake_db, equity=Decimal("1"))

async def test_read_through_a_hop():
    rows = await get_day_start_equity(FakeDB())

async def test_no_sql():
    assert describe(FakeDB()) == "a session"

async def test_real():
    async with async_session() as db:
        recorded_id = await paper_day_start_equity_recording.record_day_start_equity(db=db, equity=Decimal("1"))
'''
    got = fake_sessions({PATH: test}, MODULES)
    tail = ("a fake session that answers canned results whatever the query -- {f} (`paper_day_start_equity_recording`) "
            "runs SQL on it, and a fake cannot say whether that SQL works: the sandbox has a real scratch database -- "
            "`async with async_session() as db:`, pass that `db`, insert the rows the test needs with `text()`, and "
            "read the rows the function wrote")
    assert got == [
        "`tests/test_paper_twelve_modules.py:26` hands `record_day_start_equity(…)` `db` = a `FakeDB` instance, "
        + tail.format(f="`record_day_start_equity`"),
        "`tests/test_paper_twelve_modules.py:29` hands `get_day_start_equity(…)` `db` = a `FakeDB` instance, "
        + tail.format(f="`get_day_start_equity`")], got        # `describe` runs no SQL; the real session is right


def test_an_empty_list_the_function_fills_is_a_list_when_compared_with_a_number():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_execution_entry_point

async def test_entry(db):
    result = await paper_execution_entry_point.run_execution_entry_point(db=db, operator_id="op")
    assert result["approvals"] == 1
    assert result["orders"] == 0
    assert result["approvals"] == []
    assert result["count"] == 1
'''
    got = returned_compares({PATH: test}, MODULES)
    assert got == [
        "`tests/test_paper_twelve_modules.py:6` asserts `result['approvals'] == 1`, but `run_execution_entry_point` "
        "(`paper_execution_entry_point`) returns `[]` under `approvals` -- a list compared with a number is never equal: "
        "compare `len(result['approvals'])`",
        "`tests/test_paper_twelve_modules.py:7` asserts `result['orders'] == 0`, but `run_execution_entry_point` "
        "(`paper_execution_entry_point`) returns `[]` under `orders` -- a list compared with a number is never equal: "
        "compare `len(result['orders'])`"], got       # `== []` may hold; `count` is a number


def test_the_passes_section_is_the_engines_and_a_decimal_mixed_with_a_float_is_the_modules():
    stdout = """F..                                                                      [100%]
=================================== FAILURES ===================================
____________________________ test_a _________________________________________
E   assert False
==================================== PASSES ====================================
_____________ test_kill_switch_cancels_orders_and_closes_positions _____________
------------------------------ Captured log call -------------------------------
ERROR    scaffold:paper_risk_control_module.py:308 KILL SWITCH TRIGGERED: test kill
=========================== short test summary info ============================
PASSED tests/test_x.py::test_kill_switch_cancels_orders_and_closes_positions
PASSED tests/test_x.py::test_c
FAILED tests/test_x.py::test_a - assert False
1 failed, 2 passed in 0.94s
"""
    report = et._report(stdout)
    assert "KILL SWITCH" not in report and "PASSES" not in report and "PASSED " not in report
    assert "FAILED tests/test_x.py::test_a" in report and "E   assert False" in report
    assert et.passed_tests(stdout) == ["tests/test_x.py::test_kill_switch_cancels_orders_and_closes_positions",
                                       "tests/test_x.py::test_c"]
    rx, hint = et.KNOWN_CAUSES[10]
    assert rx.search("E   TypeError: unsupported operand type(s) for -: 'decimal.Decimal' and 'float'")
    assert rx.search("E   TypeError: unsupported operand type(s) for *: 'float' and 'decimal.Decimal'")
    assert "the module is at fault, not the test" in hint and et.KNOWN_CAUSES[10] in et.CALLEE_CAUSES
    assert et.KNOWN_CAUSES[9][0].search("Object of type Decimal is not JSON serializable")   # the order holds
    report = """
_________________________ test_get_spy_benchmark_basic _________________________
tests/test_paper_twelve_modules.py:541: in test_get_spy_benchmark_basic
    result = await paper_spy_benchmark_tracker.get_spy_benchmark(
app/modules/paper_spy_benchmark_tracker.py:146: in get_spy_benchmark
    "buy_and_hold_return": _buy_and_hold_return(prices),
app/modules/paper_spy_benchmark_tracker.py:75: in _buy_and_hold_return
    gross_return = (last_close / first_close) - 1.0
E   TypeError: unsupported operand type(s) for -: 'decimal.Decimal' and 'float'
=========================== short test summary info ============================
"""
    d = et.callee_defects(report)
    assert len(d) == 1 and d[0]["module"] == "paper_spy_benchmark_tracker" and d[0]["modules"] == ["paper_spy_benchmark_tracker"] * 2


def test_the_kept_tests_are_spliced_whatever_path_the_draw_took():
    src = inspect.getsource(ea._complete_required_tests)
    assert src.index("await _draw_required_tests(") < src.index("return _splice_kept_tests(output, node_snapshot, node_key)")
    assert "return output" not in src.split("return _splice_kept_tests")[0].split('"""')[-1]   # no early return before it
    draw = inspect.getsource(ea._draw_required_tests)
    assert "splice_kept" not in draw and draw.count("return output") >= 2            # the early returns live there now
    assert "kept_passing_tests_spliced" in inspect.getsource(ea._splice_kept_tests)
