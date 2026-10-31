"""§17.1883 — a plain `def` whose result the module awaits; a helper's parameters are the call's arguments (so a
`x if x is not None else {}` default carries the value given); `db=None` into a function that uses the session
itself; a helper call standing in for a literal argument; a `.get(…)` read the function refuses without; a list read
with a string key.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1882 round, 05:10–05:31 UTC): a1 and a4 reached the sandbox at zero
findings and failed 17 and 14. a1: `def http_get(url): return _response(…)` handed to the FINRA ingester, which awaits
it (two tests); `record_day_start_equity(db=None, …)` -- "'NoneType' object has no attribute 'execute'" (two);
`_response({"filings": {"recent": [ … ]}})` where the helper sets `resp.json.return_value = json_data if json_data is
not None else {}` and the module reads `recent.get("form")` -- the parameter was never bound, the default `{}` read
instead, "'list' object has no attribute 'get'" (five, with a4's three); `is_trade_update(_make_fill_envelope())`,
the envelope behind a helper call. a4: `parse_fill_event({"data": {"event": "fill", "qty": "1", …}})` with no
`execution_id`, which the tracker reads with `.get` and refuses without (two)."""
from __future__ import annotations

from app.modules import engine_source as es
from app.modules.engine_contracts import wrong_fakes, wrong_result_uses

FINRA = '''# app/modules/paper_finra_data_ingestion.py
async def _download_file(url: str, http_get) -> str:
    response = await http_get(url)
    response.raise_for_status()
    return response.text

async def ingest_finra_file(*, source: str, http_get) -> dict:
    text = await _download_file("https://x", http_get)
    return {"ingested_items": len(text.splitlines())}
'''

THIRTEEN_F = '''# app/modules/paper_13f_holdings_ingestion.py
async def ingest_13f_holdings(cik: str, *, db, client) -> int:
    response = await client.get("https://x/" + cik)
    response.raise_for_status()
    submissions = response.json()
    recent = submissions.get("filings", {}).get("recent", {})
    return len(recent.get("form", []))
'''

EQUITY = '''# app/modules/paper_day_start_equity_recording.py
from sqlalchemy import text

async def record_day_start_equity(*, db, equity) -> int:
    result = await db.execute(text("INSERT INTO equity (value) VALUES (:v) RETURNING id"), {"v": equity})
    await db.commit()
    return result.scalar_one()

async def get_view(*, db=None) -> dict:
    if db is not None:
        return {"rows": (await db.execute(text("SELECT 1"))).fetchall()}
    return {"rows": []}
'''

TRACKER = '''# app/modules/paper_fill_tracker.py
from typing import Any

def is_trade_update(envelope: dict[str, Any]) -> bool:
    return envelope.get("stream") == "trade_updates"

def parse_fill_event(envelope: dict[str, Any]) -> dict | None:
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    execution_id = data.get("execution_id")
    if not execution_id:
        raise ValueError("fill event missing execution_id")
    return {"execution_id": execution_id, "qty": data.get("qty")}

def list_fills(envelopes: list) -> list[dict]:
    return [e for e in envelopes if is_trade_update(e)]
'''

MODULES = {"paper_finra_data_ingestion": FINRA, "paper_13f_holdings_ingestion": THIRTEEN_F,
           "paper_day_start_equity_recording": EQUITY, "paper_fill_tracker": TRACKER}
PATH = "tests/test_paper_twelve_modules.py"


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def test_a_plain_def_whose_result_the_module_awaits_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import MagicMock
from app.modules.paper_finra_data_ingestion import ingest_finra_file

async def test_it():
    def http_get(url: str):
        return MagicMock(text="a|b")
    result = await ingest_finra_file(source="short_interest", http_get=http_get)
'''
    assert wrong_fakes({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:8` hands `ingest_finra_file(…)` `http_get` = a plain `def` (line 6), whose "
        "result the module awaits -- make it `async def` (or an `AsyncMock(return_value=…)`) -- give it what the module "
        "uses: `http_get()->{raise_for_status(), text}`"]
    assert wrong_fakes({PATH: test.replace("    def http_get(url: str):", "    async def http_get(url: str):")}, MODULES, set()) == []


def test_a_helpers_parameters_are_the_calls_arguments_so_its_default_expression_carries_the_value_given():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings

def _response(json_data=None, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    return resp

async def test_it(db):
    client = MagicMock()
    client.get = AsyncMock(return_value=_response({"filings": {"recent": [{"form": "13F-HR"}]}}))
    result = await ingest_13f_holdings("123", db=db, client=client)
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:14` hands `ingest_13f_holdings(…)` `client` = a fake whose `get()` returns a "
        "fake whose `json()` returns a dict[\"filings\"] whose `recent` is a list (line 13), which the module reads as a "
        "dict (`.get(…)` / `[\"key\"]` for `form`)"], got
    assert wrong_fakes({PATH: test.replace('"recent": [{"form": "13F-HR"}]', '"recent": {"form": ["13F-HR"]}')}, MODULES, set()) == []
    # a keyword argument binds the same way
    kw = test.replace('_response({"filings": {"recent": [{"form": "13F-HR"}]}})',
                      '_response(json_data={"filings": {"recent": [{"form": "13F-HR"}]}})')
    assert len(wrong_fakes({PATH: kw}, MODULES, set())) == 1


def test_none_for_a_session_the_function_uses_itself_is_refused_and_a_guarded_use_is_not():
    test = '''# tests/test_paper_twelve_modules.py
from decimal import Decimal
from app.modules.paper_day_start_equity_recording import record_day_start_equity, get_view

async def test_record():
    result = await record_day_start_equity(db=None, equity=Decimal("10000"))

async def test_view():
    view = await get_view(db=None)
'''
    assert wrong_fakes({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:6` hands `record_day_start_equity(…)` `db` = None, and the function uses it "
        "(`db{commit(), execute()->{scalar_one()}}`) -- `'NoneType' object has no attribute 'execute'` -- give it what "
        "the module uses: `async with async_session() as db:` and pass that `db`"]


def test_a_helper_call_stands_in_for_the_literal_it_returns():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_fill_tracker

def _make_fill_envelope() -> dict:
    return {"T": "trade_updates", "data": {"event": "fill"}}

def test_is_trade_update():
    assert paper_fill_tracker.is_trade_update(_make_fill_envelope())
'''
    defs = es.sibling_definitions([_row("T18", TRACKER)])
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs)]
    assert got == [
        "`tests/test_paper_twelve_modules.py:8` calls `is_trade_update(…)`: `envelope` has none of the keys the function "
        "reads (`stream`): it has `T`, `data`"]


def test_a_key_read_with_get_and_refused_without_is_required():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_fill_tracker import parse_fill_event

def test_fill():
    assert parse_fill_event({"stream": "trade_updates", "data": {"event": "fill", "qty": "1"}}) is not None
'''
    defs = es.sibling_definitions([_row("T18", TRACKER)])
    assert "parse_fill_event(envelope: dict{data{execution_id, qty}})" in es.block_for(
        {"node_key": "T5", "title": "t", "description": "", "prompt_template": "Write tests.", "output_text": None},
        set(), modules=defs)
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs)]
    assert got == [
        "`tests/test_paper_twelve_modules.py:5` calls `parse_fill_event(…)`: `envelope`[\"data\"] is read for "
        "`execution_id` (and refused without it) -- the dict given has `event`, `qty` and no `execution_id`"]


def test_a_list_read_with_a_string_key_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_fill_tracker import list_fills

def test_it():
    result = list_fills([])
    assert result["fills"] == 0
'''
    assert wrong_result_uses({PATH: test}, MODULES) == [
        "`tests/test_paper_twelve_modules.py:6` reads `result['fills']` from `result`, what `list_fills(…)` "
        "(`paper_fill_tracker`) returns -- and `list_fills` returns a `list`, which has no keys -- index it, or iterate it"]
