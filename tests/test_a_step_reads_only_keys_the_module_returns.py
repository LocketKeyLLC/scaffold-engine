"""§17.1850 — a step reads only keys a finished module's function returns.

Live (follow-up 5b4880c0 T6, 2026-10-28): the batch did ``context = await load_batch_context(...)`` and handed the
proposer ``equity=context.get("equity")``. The data module returns ``{day_start_equity, orders_today}`` -- never
``equity`` -- so the proposer got None and crashed in the sandbox at ``if equity <= 0``; the step's own text said the
account's equity comes from the broker (``AlpacaPaperClient.get_account()``), and §17.1834 had already shown the keys.
The static read refuses it before the sandbox, names the keys the function does return, and says where the key lives.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.modules.execution_agent  # noqa: F401 — load-bearing (real app.database)
from app.modules import engine_source as es
from app.modules.engine_contracts import unreturned_keys, unreturned_reads

DATA = '''
from app.modules.paper_day_start_equity_recording import get_day_start_equity

async def load_batch_context(*, timestamp, trading_day, db=None):
    day_start_equity = await get_day_start_equity(db=db, trading_day=trading_day)
    orders_today = 0
    return {"day_start_equity": day_start_equity, "orders_today": orders_today}
'''
CLIENT = '''
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AlpacaAccount:
    id: str
    cash: Decimal
    equity: Decimal


class AlpacaPaperClient:
    def __init__(self, *, base_url, data_url):
        self.base_url = base_url

    async def get_account(self) -> AlpacaAccount:
        data = await self._request("GET", "/v2/account")
        return AlpacaAccount(id=data["id"], cash=Decimal(data["cash"]), equity=Decimal(data["equity"]))
'''
PROPOSER = '''
async def propose_trades(signals, *, equity, day_start_equity, orders_today, db=None):
    if equity <= 0:
        return []
    return [{"ticker": s["ticker"]} for s in signals]
'''
BATCH = '''# app/modules/paper_daily_signal_proposal_batch.py
from app.modules.paper_daily_signal_proposal_batch_data import load_batch_context
from app.modules.paper_trade_proposer import propose_trades


async def run_daily_batch(*, timestamp, db=None):
    context = await load_batch_context(timestamp=timestamp, trading_day=timestamp.date(), db=db)
    proposals = await propose_trades(
        [],
        equity=context.get("equity"),
        day_start_equity=context.get("day_start_equity"),
        orders_today=context.get("orders_today", 0),
        db=db,
    )
    return len(proposals)
'''
MODULES = {"paper_daily_signal_proposal_batch_data": DATA, "paper_alpaca_paper_client": CLIENT,
           "paper_trade_proposer": PROPOSER}
FILES = {"app/modules/paper_daily_signal_proposal_batch.py": BATCH}

LIVE = ("`app/modules/paper_daily_signal_proposal_batch.py:10` reads `equity` from what `load_batch_context` "
        "(`paper_daily_signal_proposal_batch_data`) returns, and it returns dicts with keys `day_start_equity`, "
        "`orders_today` -- never `equity`. `equity` is a field of `AlpacaAccount` (`paper_alpaca_paper_client`), "
        "returned by `AlpacaPaperClient.get_account()`")


def test_the_live_read_of_a_key_the_function_never_returns_is_a_gap():
    assert unreturned_keys(BATCH, MODULES) == [{
        "producer": "paper_daily_signal_proposal_batch_data.load_batch_context", "var": "context", "key": "equity",
        "carries": ["day_start_equity", "orders_today"], "line": 10}]
    assert unreturned_reads(FILES, MODULES) == [LIVE]


def test_a_subscript_read_is_the_same_gap():
    sub = BATCH.replace('context.get("equity")', 'context["equity"]')
    assert [g["key"] for g in unreturned_keys(sub, MODULES)] == ["equity"]


def test_the_keys_it_returns_pass():
    """The correct miss: the fixed batch reads `day_start_equity` and `orders_today` -- and gets `equity` elsewhere."""
    fixed = BATCH.replace('equity=context.get("equity")', "equity=equity")
    assert unreturned_keys(fixed, MODULES) == []
    assert unreturned_reads({"app/modules/paper_daily_signal_proposal_batch.py": fixed}, MODULES) == []


def test_a_value_the_reader_cannot_follow_is_never_reported():
    """A producer returning what it was given, or spreading another dict, is unknown -- and an unknown is no claim;
    a value that is not a finished module's return is not read; a name bound twice is not followed."""
    passthrough = DATA.replace('return {"day_start_equity": day_start_equity, "orders_today": orders_today}',
                               "return given if given else {}").replace("db=None):", "db=None, given=None):")
    assert unreturned_keys(BATCH, {**MODULES, "paper_daily_signal_proposal_batch_data": passthrough}) == []
    spread = DATA.replace('{"day_start_equity"', '{**extra, "day_start_equity"').replace(
        "    orders_today = 0\n", "    orders_today = 0\n    extra = {}\n")
    assert unreturned_keys(BATCH, {**MODULES, "paper_daily_signal_proposal_batch_data": spread}) == []
    local = BATCH.replace("context = await load_batch_context(timestamp=timestamp, trading_day=timestamp.date(), db=db)",
                          'context = {"equity": 1}')
    assert unreturned_keys(local, MODULES) == []
    rebound = BATCH.replace("    proposals = await propose_trades(",
                            '    context = {**context, "equity": 1}\n    proposals = await propose_trades(')
    assert unreturned_keys(rebound, MODULES) == []
    assert unreturned_keys(BATCH, {}) == []                                   # the producer is not a finished module


def test_where_the_key_lives_is_read_from_the_finished_modules():
    """A dict-returning function that carries the key is named too; no hint when nothing holds it."""
    dicts = DATA.replace("async def load_batch_context", "async def account_snapshot(*, db=None):\n"
                         '    return {"equity": 1, "cash": 2}\n\n\nasync def load_batch_context')
    out = unreturned_reads(FILES, {**MODULES, "paper_daily_signal_proposal_batch_data": dicts})
    assert len(out) == 1 and out[0].endswith(
        "`equity` is a field of `AlpacaAccount` (`paper_alpaca_paper_client`), returned by "
        "`AlpacaPaperClient.get_account()`; `paper_daily_signal_proposal_batch_data.account_snapshot()` returns "
        "dicts with it")
    nowhere = unreturned_reads(FILES, {k: v for k, v in MODULES.items() if k != "paper_alpaca_paper_client"})
    assert len(nowhere) == 1 and nowhere[0].endswith("-- never `equity`")


T6D = {"node_key": "T6D", "title": "Build daily signal-to-proposal batch -- data", "description": "",
       "prompt_template": "Python module `paper_daily_signal_proposal_batch_data.py`.", "status": "done",
       "output_text": f"```python\n# app/modules/paper_daily_signal_proposal_batch_data.py\n{DATA}```"}
T5 = {"node_key": "f00ea97f:T5", "title": "Alpaca paper client", "description": "",
      "prompt_template": "Python module `paper_alpaca_paper_client.py`.", "status": "done",
      "output_text": f"```python\n# app/modules/paper_alpaca_paper_client.py\n{CLIENT}```"}
T1F2 = {"node_key": "T1F2", "title": "Trade proposer", "description": "",
        "prompt_template": "Python module `paper_trade_proposer.py`.", "status": "done",
        "output_text": f"```python\n# app/modules/paper_trade_proposer.py\n{PROPOSER}```"}
ME = {"node_key": "T6", "title": "Build daily signal-to-proposal batch", "description": "",
      "prompt_template": "Python module `paper_daily_signal_proposal_batch.py`.", "status": "running", "output_text": ""}


@pytest.mark.asyncio
async def test_the_gate_refuses_the_live_batch_and_passes_the_fixed_one():
    """§17.1850 bites at the production chokepoint (`engine_findings`), in the refusal the next attempt is drafted from."""
    out = f"```python\n{BATCH}```"
    for attempt, text_ in ((out, LIVE), (out.replace('equity=context.get("equity")', "equity=equity"), None)):
        db = AsyncMock()
        nodes = MagicMock(); nodes.mappings.return_value.all.return_value = [T5, T1F2, T6D, ME]
        cols = MagicMock(); cols.all.return_value = [("jobs", "id")]
        db.execute = AsyncMock(side_effect=[nodes, cols])
        found = await es.engine_findings(db, "job-1", "T6", attempt)
        if text_:
            assert text_ in found, found
            assert text_ in es.unresolved_reason(found)
        else:
            assert not any("never `equity`" in f for f in found), found
