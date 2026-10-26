"""§17.1833 — what one finished module hands another carries the keys the other requires.

Live (follow-up 5b4880c0 T6, 2026-10-26): the batch handed `load_batch_signals()` (a pass-through to f00ea97f's
`generate_signals`) to `propose_trades`, which skips every signal without `signal_id` (or `id`). The generator's dicts
are never saved and carry no id, so every batch proposed nothing; T6 failed three rounds at a gap in two other modules.
The shapes below are the live modules', cut to what the check reads.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import node_editor
from app.modules.engine_contracts import contract_gaps, required_keys
from app.modules.execution_retry import retry_failed_node

text = node_editor.text

GENERATOR = '''
from app.modules.paper_point_time_store import get_data_as_of

def _normalize_payload(payload, source):
    ticker = payload.get("ticker")
    return {"source": source, "ticker": ticker, "signal_type": "news", "direction": "long",
            "rationale": "r", "published_ts": payload.get("published_ts") or ""}

def normalize_edgar_payload(payload):
    return {"source": "sec_edgar", "ticker": payload["issuer"], "signal_type": "insider", "direction": "long",
            "rationale": "r", "published_ts": ""}

def _normalize_item(item):
    source = item.get("source")
    payload = item.get("payload") or {}
    if source == "sec_edgar":
        return normalize_edgar_payload(payload)
    normalized = _normalize_payload(payload, source)
    if not normalized["published_ts"]:
        normalized["published_ts"] = item.get("published_ts") or ""
    return normalized

async def generate_signals(timestamp, *, db=None):
    items = await get_data_as_of(timestamp, db=db)
    weighted: list = []
    for index, item in enumerate(items):
        normalized = _normalize_item(item)
        weighted.append((1, index, normalized))
    weighted.sort(key=lambda e: e[1])
    return [entry[2] for entry in weighted]
'''
DATA = '''
from app.modules.paper_signal_generator import generate_signals

async def load_batch_signals(*, timestamp, db=None):
    return await generate_signals(timestamp, db=db)
'''
PROPOSER = '''
async def propose_trades(signals, *, equity, day_start_equity, orders_today, db=None):
    out = []
    for signal in signals:
        ticker = str(signal.get("ticker", ""))
        signal_id = signal.get("signal_id") or signal.get("id")
        if signal_id is None:
            continue
        limit = signal.get("limit_price")
        if limit is None:
            limit = 0
        out.append({"signal_id": signal_id, "ticker": ticker, "limit": limit})
    return out
'''
BATCH = '''# app/modules/paper_daily_signal_proposal_batch.py
from app.modules.paper_daily_signal_proposal_batch_data import load_batch_signals
from app.modules.paper_trade_proposer import propose_trades

async def run_daily_signal_proposal_batch(*, timestamp, db=None):
    signals = await load_batch_signals(timestamp=timestamp, db=db)
    proposals = await propose_trades(signals, equity=1, day_start_equity=1, orders_today=0, db=db)
    return len(proposals)
'''
MODULES = {"paper_signal_generator": GENERATOR, "paper_daily_signal_proposal_batch_data": DATA,
           "paper_trade_proposer": PROPOSER}


def test_a_key_the_consumer_requires_and_no_producer_dict_carries_is_a_gap():
    gaps = contract_gaps(BATCH, MODULES)
    assert gaps == [{
        "producer": "paper_daily_signal_proposal_batch_data.load_batch_signals",
        "consumer": "paper_trade_proposer.propose_trades", "param": "signals", "missing": [["id", "signal_id"]],
        "carries": ["direction", "published_ts", "rationale", "signal_type", "source", "ticker"],
        "origin": ["paper_signal_generator"]}]


def test_the_gap_closes_when_the_dicts_carry_the_key():
    """The correct miss: the fix step's version -- each signal saved, its id carried -- is not reported. A key any
    return path carries is no claim (the reader is conservative: a union, never a guess at which path runs)."""
    fixed = GENERATOR.replace('"published_ts": payload.get("published_ts") or ""}',
                              '"published_ts": payload.get("published_ts") or "", "signal_id": 7}')
    assert contract_gaps(BATCH, {**MODULES, "paper_signal_generator": fixed}) == []
    assert contract_gaps(BATCH.replace("propose_trades(signals,", "propose_trades([],"), MODULES) == []   # not handed on


def test_a_key_stored_into_the_dict_later_is_carried():
    stored = GENERATOR.replace('    return normalized\n', '    normalized["signal_id"] = 1\n    return normalized\n')
    assert contract_gaps(BATCH, {**MODULES, "paper_signal_generator": stored}) == []


def test_a_producer_it_cannot_read_is_never_reported():
    param = DATA.replace("async def load_batch_signals(*, timestamp, db=None):\n    return await generate_signals("
                         "timestamp, db=db)", "async def load_batch_signals(*, timestamp, db=None, given=()):\n"
                         "    if given:\n        found = given\n        return found\n    return await generate_signals(timestamp, db=db)")
    assert param != DATA and contract_gaps(BATCH, {**MODULES, "paper_daily_signal_proposal_batch_data": param}) == []
    rows = GENERATOR.replace("return [entry[2] for entry in weighted]", "return [dict(r) for r in items]")
    assert contract_gaps(BATCH, {**MODULES, "paper_signal_generator": rows}) == []
    spread = GENERATOR.replace('return {"source": source,', 'return {**payload, "source": source,')
    assert contract_gaps(BATCH, {**MODULES, "paper_signal_generator": spread}) == []


def test_only_a_key_it_gives_up_without_is_required():
    import ast
    fn = ast.parse(PROPOSER).body[0]
    assert required_keys(fn, "signals") == [frozenset({"signal_id", "id"})]          # `limit_price` is optional
    sub = ast.parse(PROPOSER.replace('ticker = str(signal.get("ticker", ""))', 'ticker = signal["ticker"]')).body[0]
    assert frozenset({"ticker"}) in required_keys(sub, "signals")


BRIEF = {"title": "Paper-trading lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}


def _block(mod: str, code: str) -> str:
    return f"```python\n# app/modules/{mod}.py\n{code}\n```"


async def _job(db) -> str:
    jid = str(uuid.uuid4())
    await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                          "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(BRIEF)})
    for i, (k, st, prompt, out) in enumerate([
            ("T1F4", "done", "Python module `paper_signal_generator.py`.", _block("paper_signal_generator", GENERATOR)),
            ("T1F2", "done", "Python module `paper_trade_proposer.py`.", _block("paper_trade_proposer", PROPOSER)),
            ("T6D", "done", "Python module `paper_daily_signal_proposal_batch_data.py`.",
             _block("paper_daily_signal_proposal_batch_data", DATA)),
            ("T6", "failed", "Python module `paper_daily_signal_proposal_batch.py`. The batch.", f"```python\n{BATCH}```")]):
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, depends_on, prompt_template, output_text, tool, "
            "execution_order, retry_count, max_retries) VALUES (:j, :k, :k, :s, :d, :p, :o, 'CodeGen', :n, :rc, 3)"),
            {"j": jid, "k": k, "s": st, "d": ["T6D"] if k == "T6" else [], "p": prompt, "o": out, "n": i,
             "rc": 3 if k == "T6" else 0})
    await db.commit()
    return jid


async def _drop(jid: str) -> None:
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM execution_logs WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


@pytest.mark.integration
async def test_an_exhausted_step_handing_on_a_gap_waits_for_the_module_that_builds_the_dicts():
    from app.database import async_session
    async with async_session() as db:
        jid = await _job(db)
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T6", db)
        assert out["status"] == "reset" and out["upstream_fix"] == {
            "node_key": "T6", "fix_steps": ["T6U1"], "modules": ["paper_signal_generator"]}
        async with async_session() as db:
            rows = {r["node_key"]: r for r in (await db.execute(text(
                "SELECT node_key, status, depends_on, prompt_template, tool_config FROM dag_nodes WHERE job_id = :j"),
                {"j": jid})).mappings().all()}
        fix = rows["T6U1"]
        assert fix["tool_config"] == {
            "fixes_module": "paper_signal_generator", "found_by": "T6", "owner": "T1F4",
            "contract": "paper_trade_proposer.propose_trades.signals:id/signal_id",
            "gaps": [{"producer": "paper_daily_signal_proposal_batch_data.load_batch_signals",
                      "consumer": "paper_trade_proposer.propose_trades", "param": "signals",
                      "missing": [["id", "signal_id"]],
                      "carries": ["direction", "published_ts", "rationale", "signal_type", "source", "ticker"]}]}
        # §17.1835 — saved once, and the modules that call it named
        assert "Save each item ONCE: an item already saved keeps its row and its id" in fix["prompt_template"]
        assert "Other finished modules call it and get the same: `paper_daily_signal_proposal_batch_data` " \
               "(`generate_signals`) -- each must keep working." in fix["prompt_template"]
        assert "calls it twice over the same items and finds one row per item" in fix["prompt_template"]
        assert fix["prompt_template"].startswith("Python module `paper_signal_generator.py` -- step T1F4's module")
        assert "`paper_trade_proposer.propose_trades` requires each item of its `signals` to carry `id` or " \
               "`signal_id`" in fix["prompt_template"]
        assert "an id names a saved row" in fix["prompt_template"]
        assert list(rows["T6"]["depends_on"]) == ["T6D", "T6U1"]
        async with async_session() as db:              # asked once: exhausted again, the same gap adds nothing
            await db.execute(text("UPDATE dag_nodes SET status = CASE node_key WHEN 'T6' THEN 'failed' ELSE 'done' "
                                  "END, retry_count = CASE node_key WHEN 'T6' THEN 3 ELSE 0 END, output_text = CASE "
                                  "node_key WHEN 'T6' THEN :o ELSE output_text END WHERE job_id = :j "
                                  "AND node_key IN ('T6', 'T6U1')"), {"j": jid, "o": f"```python\n{BATCH}```"})
            await db.commit()
        async with async_session() as db:
            again = await retry_failed_node(jid, "T6", db)
        assert "upstream_fix" not in again, again
    finally:
        await _drop(jid)


# ── §17.1835 — a contract fix step made before the save-once rule is re-rendered when asked again ──────────────

LEGACY = (  # T6U6's text as §17.1833 wrote it (live, 5b4880c0)
    "Python module `paper_signal_generator.py` -- step T1F4's module: edit it in place (the whole file, its public "
    "names kept). What it returns is handed on to a function that cannot use it:\n"
    "- `paper_trade_proposer.propose_trades` requires each item of its `signals` to carry `id` or `signal_id` (it "
    "skips or fails an item without); step T6 hands it what `paper_daily_signal_proposal_batch_data.load_batch_signals` "
    "returns, and every dict this module builds for that carries only `direction`, `published_ts`, `rationale`, "
    "`signal_type`, `source`, `ticker`.\nMake every dict it returns carry the key, from where the value really comes "
    "from: an id names a saved row -- save the item with the finished module that writes that table and carry the id "
    "it returns, with whatever that write needs; never a made-up value. In this same step also write "
    "`tests/test_paper_signal_generator.py`: ...")


@pytest.mark.integration
async def test_a_contract_fix_step_asked_again_is_rerendered_with_the_save_once_rule():
    from app.database import async_session
    from app.modules.engine_plan import refresh_contract_fix
    async with async_session() as db:
        jid = await _job(db)
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, tool, tool_config, execution_order) "
            "VALUES (:j, 'T6U6', 'Fix', 'done', :p, 'CodeGen', CAST(:c AS jsonb), 9)"),
            {"j": jid, "p": LEGACY, "c": json.dumps({"fixes_module": "paper_signal_generator", "found_by": "T6",
                                                      "contract": "paper_trade_proposer.propose_trades.signals:id/signal_id"})})
        await db.commit()
    try:
        async with async_session() as db:
            out = await refresh_contract_fix(db, jid, "T6U6", "operator")
            await db.commit()
        assert out == {"node_key": "T6U6", "refreshed": True}
        async with async_session() as db:
            r = (await db.execute(text("SELECT prompt_template, tool_config, status FROM dag_nodes WHERE job_id = :j AND "
                                       "node_key = 'T6U6'"), {"j": jid})).mappings().first()
        assert r["prompt_template"].startswith(LEGACY.split("Make every dict")[0])          # the gap kept, read back
        assert "Save each item ONCE" in r["prompt_template"]
        assert "`paper_daily_signal_proposal_batch_data` (`generate_signals`)" in r["prompt_template"]
        assert r["tool_config"]["owner"] == "T1F4" and r["tool_config"]["gaps"][0]["missing"] == [["id", "signal_id"]]
        assert r["tool_config"]["gaps"][0]["carries"][-1] == "ticker"
        async with async_session() as db:              # asked again with the text current: unchanged
            assert await refresh_contract_fix(db, jid, "T6U6", "operator") == {"node_key": "T6U6", "refreshed": False}
            assert await refresh_contract_fix(db, jid, "T6", "operator") is None              # not a contract step
    finally:
        await _drop(jid)
