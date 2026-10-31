"""§17.1879 — a dict is read level by level: a literal with none of the keys a level reads, or a list where a dict is
read, is refused -- for a parameter and for a fake's `json()` alike; a fake with fields set but not the one the
module reads; a key that would steer a function which never reads it.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1878 round, 00:39–00:53 UTC): three attempts reached the sandbox at
zero findings and failed 11, 15 and 20 tests. Every run: `is_trade_update({"T": "trade_updates"})` where the tracker
reads `stream`, `parse_fill_event({"T": …, "msg": {…}})` where it reads `data` (four tests); `json()` ->
`{"filings": [...]}` where `_find_13f_hr_rows` reads `.get("filings", {}).get("recent")` (two); `MagicMock(content=…)`
where the FINRA downloader returns `response.text` into `io.StringIO` (two); `execute_order({"decision": "rejected",
…})` asserted a refusal from a function that reads `approval_id`, `proposal`, `proposal_id` and nothing else (six)."""
from __future__ import annotations

import ast

from app.modules import engine_source as es
from app.modules.engine_contracts import wrong_fakes

TRACKER = '''# app/modules/paper_fill_tracker.py
from typing import Any

def is_trade_update(envelope: dict[str, Any]) -> bool:
    return envelope.get("stream") == "trade_updates"

def parse_fill_event(envelope: dict[str, Any]) -> dict | None:
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("event") not in ("fill", "partial_fill"):
        return None
    return {"qty": data["qty"], "order": data.get("order", {}).get("id")}
'''

THIRTEEN_F = '''# app/modules/paper_13f_holdings_ingestion.py
import httpx

async def _fetch_submissions(client, cik: str) -> dict:
    response = await client.get("https://x/" + cik)
    response.raise_for_status()
    return response.json()

def _find_13f_hr_rows(submissions: dict) -> list:
    recent = submissions.get("filings", {}).get("recent", {})
    return list(recent.get("form", []))

def _ticker(submissions: dict) -> str:
    return (submissions.get("tickers") or [""])[0]

async def ingest_13f_holdings(cik: str, *, client) -> int:
    submissions = await _fetch_submissions(client, cik)
    _ticker(submissions)
    return len(_find_13f_hr_rows(submissions))
'''

FINRA = '''# app/modules/paper_finra_data_ingestion.py
import io, csv

async def _download_file(url: str, http_get) -> str:
    response = await http_get(url)
    response.raise_for_status()
    return response.text

async def ingest_finra_file(*, source: str, http_get) -> dict:
    content = await _download_file("https://x", http_get)
    rows = list(csv.reader(io.StringIO(content), delimiter="|"))
    return {"ingested_items": len(rows)}
'''

EXECUTOR = '''# app/modules/paper_order_executor.py
async def execute_order(approval: dict) -> dict:
    proposal = approval["proposal"]
    return {"status": "accepted", "approval_id": approval["approval_id"], "symbol": proposal["symbol"],
            "proposal_id": approval.get("proposal_id")}
'''

MODULES = {"paper_fill_tracker": TRACKER, "paper_13f_holdings_ingestion": THIRTEEN_F,
           "paper_finra_data_ingestion": FINRA, "paper_order_executor": EXECUTOR}
PATH = "tests/test_paper_twelve_modules.py"


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def _calls(test: str) -> list[str]:
    defs = es.sibling_definitions([_row("T2", TRACKER), _row("T3", EXECUTOR)])
    return [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs, set())]


def test_a_literal_with_none_of_the_keys_a_level_reads_is_refused_and_a_raise_may_provoke_it():
    test = '''# tests/test_paper_twelve_modules.py
import pytest
from app.modules.paper_fill_tracker import is_trade_update, parse_fill_event

def test_stream():
    assert is_trade_update({"T": "trade_updates"}) is True

def test_fill():
    assert parse_fill_event({"T": "trade_updates", "msg": {"event": "fill"}}) is not None

def test_fill_data():
    assert parse_fill_event({"stream": "trade_updates", "data": {"kind": "fill"}}) is None

def test_raises():
    with pytest.raises(KeyError):
        parse_fill_event({"data": {"event": "fill", "order": {}}})
'''
    assert _calls(test) == [
        # `stream == "trade_updates"` is returned, not gated on (§17.1881): "none of the keys" it is
        "`tests/test_paper_twelve_modules.py:6` calls `is_trade_update(…)`: `envelope` has none of the keys the "
        "function reads (`stream`): it has `T`",
        "`tests/test_paper_twelve_modules.py:9` calls `parse_fill_event(…)`: `envelope` has none of the keys the "
        "function reads (`data`): it has `T`, `msg`",
        # one level down, a key compared with values and one read as `["qty"]` say more than "none of the keys"
        "`tests/test_paper_twelve_modules.py:12` calls `parse_fill_event(…)`: `envelope`[\"data\"] is read for `event` "
        "(one of `fill`, `partial_fill`; without it the function takes the other way) -- the dict given has `kind` "
        "and no `event`",
        "`tests/test_paper_twelve_modules.py:12` calls `parse_fill_event(…)`: `envelope`[\"data\"] is read for `qty` "
        "(as `[\"qty\"]`, which raises without it) -- the dict given has `kind` and no `qty`",
        # line 16: `qty` is read as `["qty"]` and missing -- the test expects the KeyError: not refused
    ]
    fine = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_fill_tracker import is_trade_update, parse_fill_event

def test_ok():
    assert is_trade_update({"stream": "trade_updates", "T": "x"}) is True
    assert parse_fill_event({"stream": "trade_updates", "data": {"event": "fill", "qty": "1", "order": {"id": "o"}}})
'''
    assert _calls(fine) == []


def test_a_list_under_a_key_the_function_reads_as_a_dict_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_fill_tracker import parse_fill_event

def test_fill():
    parse_fill_event({"data": {"event": "fill", "qty": "1", "order": ["o-1"]}})
'''
    assert _calls(test) == [
        "`tests/test_paper_twelve_modules.py:5` calls `parse_fill_event(…)`: `envelope`[\"data\"][\"order\"] is read as "
        "a dict (its `id`); it was given a `list`"]


def test_the_index_and_the_fakes_gate_read_a_json_result_level_by_level():
    tree = ast.parse(THIRTEEN_F)
    local = es._local_functions(tree)
    assert es._uses_text(es._uses(local["ingest_13f_holdings"], "client", local)) == \
        "{get()->{json(): dict{filings{recent{form}}|tickers}, raise_for_status()}}"
    head = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings

async def test_it():
    response = MagicMock()
    response.json.return_value = PAYLOAD
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    assert await ingest_13f_holdings("0001", client=client) >= 0
'''

    def refused(payload: str) -> list[str]:
        return [f.split(" -- give it")[0].split("= a fake", 1)[1] for f in
                wrong_fakes({PATH: head.replace("PAYLOAD", payload)}, MODULES, set())]

    assert refused('{"filings": [{"form": "13F-HR"}]}') == [
        " whose `get()` returns a fake whose `json()` returns a dict whose `filings` is a list (line 7), which the "
        "module reads as a dict (`.get(…)` / `[\"key\"]` for `recent`)"]
    assert refused('{"filings": {"items": []}}') == [
        " whose `get()` returns a fake whose `json()` returns a dict[\"filings\"] with keys `items` (line 7), which "
        "the module reads for `recent` -- none of them is there"]
    assert refused('{"submissions": {}}') == [
        " whose `get()` returns a fake whose `json()` returns a dict with keys `submissions` (line 7), which the "
        "module reads for `filings` / `tickers` -- none of them is there"]
    assert refused('{"filings": {"recent": {"form": ["13F-HR"]}}, "tickers": ["AAPL"]}') == []
    assert refused('{}') == []


def test_a_fake_with_fields_set_but_not_the_one_the_module_reads_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_finra_data_ingestion import ingest_finra_file

async def test_it():
    fake_http_get = AsyncMock(return_value=MagicMock(content="a|b"))
    result = await ingest_finra_file(source="short_interest", http_get=fake_http_get)
'''
    assert wrong_fakes({PATH: test}, MODULES, set()) == [
        "`tests/test_paper_twelve_modules.py:7` hands `ingest_finra_file(…)` `http_get` = a fake whose call returns a "
        "fake with `content` set (line 6), where the module reads `.text` -- set `fake.text = …` too (the index shows "
        "what it reads) -- give it what the module uses: `http_get()->{raise_for_status(), text}`"]
    # set through chains too; a bare mock, or one with `.text` set, is read as before
    chained = test.replace("fake_http_get = AsyncMock(return_value=MagicMock(content=\"a|b\"))",
                           "resp = MagicMock()\n    resp.status_code = 200\n    fake_http_get = AsyncMock(return_value=resp)")
    assert len(wrong_fakes({PATH: chained}, MODULES, set())) == 1
    assert wrong_fakes({PATH: test.replace('MagicMock(content="a|b")', 'MagicMock(text="a|b")')}, MODULES, set()) == []
    assert wrong_fakes({PATH: test.replace('MagicMock(content="a|b")', 'MagicMock()')}, MODULES, set()) == []


def test_a_key_that_would_steer_a_function_which_never_reads_it_is_named():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_order_executor import execute_order

async def test_refuses():
    result = await execute_order({"approval_id": 1, "proposal_id": 2, "proposal": {"symbol": "SPY"}, "decision": "rejected"})
    assert result["status"] == "rejected"
'''
    assert _calls(test) == [
        "`tests/test_paper_twelve_modules.py:5` calls `execute_order(…)`: `approval[\"decision\"]` is never read by "
        "`execute_order` (it reads `approval_id`, `proposal`, `proposal_id`): it does what it does whatever `decision` "
        "says -- a refusal lives in the function that reads such a key (the index shows each function's keys)"]
    assert _calls(test.replace(', "decision": "rejected"', ', "note": "x"')) == []
