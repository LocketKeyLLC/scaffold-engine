"""§17.1881 — an attribute the module reads for its truth is not a mock; a key is compared with values the index
shows; an attribute set on an instance of a job class is held to what its methods do with it; a dict with known
keys is never compared with `{}`; the prompt optimizer leaves engine code alone; the trace cap is 1 MB.

Live (follow-up 5b4880c0 T5, 2026-10-31, the §17.1880 round's parked items): three Alpaca tests set
`client._client = AsyncMock()` on a real `AlpacaPaperClient`, whose `_ensure_client` reads `if self._client is None
or self._client.is_closed:` -- a mock attribute is truthy, a real `httpx.AsyncClient` opened, the network reached;
eight tests a run asserted `>= 1` row from fakes whose records the parsers drop on a field value
(`disclosure_type not in ("P", "S")`, `form != "4"`); the operator view's test compared its dict with `{}`; the
prompt optimizer restated "mock `async_session`" on engine code and twice returned nothing."""
from __future__ import annotations

import ast
import inspect

from app.modules import engine_source as es
from app.modules import execution_agent as ea
from app.modules.engine_contracts import wrong_fakes, wrong_result_uses

ALPACA = '''# app/modules/paper_alpaca_paper_client.py
import httpx

class AlpacaPaperClient:
    def __init__(self, base_url: str = "https://paper") -> None:
        self._client = None
        self._base_url = base_url

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self._base_url)
        return self._client

    async def get_account(self) -> dict:
        client = await self._ensure_client()
        response = await client.request("GET", "/v2/account")
        return response.json()
'''

CONGRESS = '''# app/modules/paper_congressional_trade_ingestion.py
async def _fetch_json(client, url: str) -> dict:
    response = await client.get(url)
    return response.json()

def _parse_row(raw: dict) -> dict | None:
    disclosure_type = str(raw.get("filing_type", raw.get("type", ""))).strip().upper()
    if disclosure_type not in ("P", "S"):
        return None
    if raw.get("chamber") == "house":
        return {"ticker": raw["ticker"], "chamber": "house"}
    return {"ticker": raw["ticker"], "chamber": raw.get("chamber")}

async def fetch_disclosures(client) -> list:
    data = await _fetch_json(client, "https://x")
    rows = []
    for raw in data.get("results") or []:
        row = _parse_row(raw)
        if row:
            rows.append(row)
    return rows
'''

VIEW = '''# app/modules/paper_operator_view.py
async def get_operator_view(db) -> dict:
    return {"cumulative_return": None, "max_drawdown": None, "open_positions": []}
'''

MODULES = {"paper_alpaca_paper_client": ALPACA, "paper_congressional_trade_ingestion": CONGRESS, "paper_operator_view": VIEW}
PATH = "tests/test_paper_twelve_modules.py"


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


def test_an_attribute_set_on_an_instance_is_held_to_what_the_methods_do_with_it():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def test_get_account_parses():
    client = AlpacaPaperClient(base_url="http://fake")
    mock_response = MagicMock()
    mock_response.json.return_value = {"id": "acct"}
    client._client = AsyncMock()
    client._client.request = AsyncMock(return_value=mock_response)
    account = await client.get_account()
'''
    got = wrong_fakes({PATH: test}, MODULES, set())
    assert [g.split(" -- give it")[0] for g in got] == [
        "`tests/test_paper_twelve_modules.py:9` sets `_client` on a `AlpacaPaperClient` instance to a fake whose "
        "`.is_closed` is a mock (line 9): the module tests it (`if … .is_closed`), and a mock attribute is always truthy "
        "-- set `fake.is_closed = False` (or what the real one would say)"], got
    assert got[0].endswith("-- give it what the module uses: `_client{request()->{json()}, is_closed}`"), got   # §17.1886 the getter's callers too
    fixed = test.replace("client._client = AsyncMock()\n", "client._client = AsyncMock()\n    client._client.is_closed = False\n")
    assert wrong_fakes({PATH: fixed}, MODULES, set()) == []
    # the tree knows which attributes are read for their truth
    tree = ast.parse(ALPACA)
    cls = next(c for c in tree.body if isinstance(c, ast.ClassDef))
    methods = {m.name: m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    u = es._uses(methods["_ensure_client"], "self._client", methods)
    assert u["tested"] == {"is_closed"} and u["attrs"] == {"is_closed"}


def test_a_key_compared_with_values_is_shown_and_a_record_outside_them_is_refused():
    tree = ast.parse(CONGRESS)
    local = es._local_functions(tree)
    assert es._uses_text(es._uses(local["fetch_disclosures"], "client", local)) == \
        "{get()->{json(): dict{results[dict{chamber|filing_type:P|S|ticker|type}]}}}"
    head = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures

async def test_it():
    response = MagicMock()
    response.json.return_value = {"results": [ROW]}
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    assert len(await fetch_disclosures(client)) == 1
'''

    def refused(row: str) -> list[str]:
        return [f.split(" -- give it")[0].split("json()` returns ", 1)[1] for f in
                wrong_fakes({PATH: head.replace("ROW", row)}, MODULES, set())]

    assert refused('{"ticker": "AAPL", "filing_type": "purchase", "chamber": "house"}') == [
        "a dict[\"results\"][0] whose `filing_type` is 'purchase' (line 7), and the module takes only `P` / `S` there"]
    assert refused('{"ticker": "AAPL", "chamber": "house"}') == [
        "a dict[\"results\"][0] without `filing_type` (line 7), which the module compares with `P` / `S` -- it takes the "
        "other way without it"]
    assert refused('{"ticker": "AAPL", "filing_type": "p", "chamber": "senate"}') == []     # upper-cased before compared
    assert refused('{"ticker": "AAPL", "type": "S"}') == []


def test_a_dict_with_known_keys_is_never_compared_with_an_empty_one():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_operator_view import get_operator_view

async def test_empty(db):
    result = await get_operator_view(db)
    assert result == {}

async def test_keys(db):
    result = await get_operator_view(db)
    assert result["open_positions"] == []
    assert result != {}
'''
    assert wrong_result_uses({PATH: test}, MODULES) == [
        "`tests/test_paper_twelve_modules.py:6` compares `result` (`result == {}`), what `get_operator_view(…)` "
        "(`paper_operator_view`) returns -- and `get_operator_view` returns a dict with keys `cumulative_return`, "
        "`max_drawdown`, `open_positions` -- never an empty one: assert on those keys"]


def test_the_optimizer_leaves_engine_code_alone_and_the_trace_cap_is_a_megabyte():
    src = inspect.getsource(ea.execute_next_node)
    assert "_engine_code = (tool or \"\").lower() == \"codegen\" and _es0.targets_the_engine(brief)" in src
    assert "and not _inventory_text and not _engine_code" in src
    from app.config import Settings
    assert Settings.model_fields["trace_capture_max_chars"].default == 8000
    assert Settings.model_fields["trace_capture_max_chars"].metadata[1].le == 1_000_000


PROPOSER = '''# app/modules/paper_trade_proposer.py
from typing import Any
from sqlalchemy import text

async def _record_rejected_proposal(db, entry: dict[str, Any]) -> None:
    await db.execute(text("INSERT INTO proposal (signal_id, ticker) VALUES (:signal_id, :ticker)"),
                     {"signal_id": entry["signal_id"], "ticker": entry["ticker"]})

async def propose_trades(signals: list[dict[str, Any]], *, db=None) -> list[dict]:
    rejected = []
    for signal in signals:
        signal_id = signal.get("signal_id") or signal.get("id")
        if signal_id is None:
            continue
        rejected.append({"signal_id": signal_id, "ticker": signal["ticker"]})
    for entry in rejected:
        await _record_rejected_proposal(db, entry)
    return rejected
'''

FETCH = '''# app/modules/paper_congressional_trade_ingestion.py
async def _fetch_json(client, url: str) -> dict:
    response = await client.get(url)
    return response.json()

async def fetch_disclosures(client) -> list:
    data = await _fetch_json(client, "https://x")
    records = data.get("results") or data.get("data") or data.get("filings") or []
    rows = []
    for raw in records:
        rows.append({"ticker": raw.get("asset_ticker"), "date": raw.get("transaction_date")})
    return rows
'''


def test_a_list_parameter_is_read_item_by_item_down_to_the_column_its_key_is_written_to():
    tree = ast.parse(PROPOSER)
    local = es._local_functions(tree)
    keys = es._param_keys(local["propose_trades"], local)
    assert keys["signals"]["[]"]["sub"]["signal_id"] == {"to": "proposal.signal_id", "needed": True}   # §17.1883 `if … is None: continue`
    assert es._keys_text(keys["signals"]) == "[{id, signal_id, ticker}]"
    defs = es.sibling_definitions([_row("T6", PROPOSER)])
    fks = {"proposal": {"signal_id": "signal.id"}}
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_trade_proposer import propose_trades

async def test_blocks(db):
    signals = [{"signal_id": 1, "ticker": "SPY", "signal_type": "form4"}]
    result = await propose_trades(signals, db=db)

async def test_real(db, signal_id):
    result = await propose_trades([{"signal_id": signal_id, "ticker": "SPY"}], db=db)
'''
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs, fks)]
    assert got == [
        "`tests/test_paper_twelve_modules.py:6` calls `propose_trades(…)`: `signals`[0][\"signal_id\"] is written to "
        "`proposal.signal_id`, which references `signal`; it was given the number 1 -- a made-up id the scratch "
        "database refuses: pass the id its parent's writer returned"], got


def test_a_dict_under_a_key_the_module_iterates_as_a_list_is_refused_whichever_alternative_key_carries_it():
    tree = ast.parse(FETCH)
    local = es._local_functions(tree)
    u = es._uses(local["fetch_disclosures"], "client", local)
    keys = u["calls"]["get"]["then"]["calls"]["json"]["then"]["keys"]
    assert keys["results"]["same_as"] == ["data", "filings"] and keys["results"]["as"] == "list"
    head = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures

def _fake_response(rows):
    return PAYLOAD

async def test_it():
    rows = [{"asset_ticker": "AAPL", "transaction_date": "2026-10-01"}]
    response = MagicMock()
    response.json.return_value = _fake_response(rows)
    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    assert len(await fetch_disclosures(client)) == 1
'''
    mods = {"paper_congressional_trade_ingestion": FETCH}

    def refused(payload: str) -> list[str]:
        return [f.split(" -- give it")[0].split("json()` returns ", 1)[1] for f in
                wrong_fakes({PATH: head.replace("PAYLOAD", payload)}, mods, set())]

    assert refused('{"data": {"filings": {"results": rows}}}') == [
        "a dict whose `data` is a dict (line 6), which the module iterates as a list and reads each item as a dict "
        "(`asset_ticker` / `transaction_date`) -- iterating a dict yields its keys, strings"]
    assert refused('{"filings": {"results": rows}}') == [
        "a dict whose `filings` is a dict (line 6), which the module iterates as a list and reads each item as a dict "
        "(`asset_ticker` / `transaction_date`) -- iterating a dict yields its keys, strings"]
    assert refused('{"data": rows}') == [] and refused('{"results": []}') == []
