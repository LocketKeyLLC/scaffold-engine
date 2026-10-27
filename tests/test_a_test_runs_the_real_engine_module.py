"""§17.1836 — a test does not stand in for a finished module that never leaves the engine.

Live (follow-up 5b4880c0 T6U6, 2026-10-26): the generator's tests replaced `get_data_as_of` with a fake "as
get_data_as_of would" returning `"id": row[0]`. The real one returns only each row's payload (`SELECT payload FROM
ingested_item ...`), so the fix read `item.get("id")`, passed the sandbox, and on real data gives every signal `id: None`.
On both lab jobs' real outputs: 12 such stand-ins (the earlier job's approval step faked `generate_signals`, which is how
its signal-id gap went unseen); every mock of the broker client, the model-backed proposer and the backtester (which
reaches the broker through the SPY tracker) is allowed.
"""
from __future__ import annotations

import pytest

from app.modules.engine_contracts import internal_fakes, reaches_outside

STORE = '''from sqlalchemy import text
from app.database import async_session

async def get_data_as_of(timestamp, *, db=None):
    rows = (await db.execute(text("SELECT payload FROM ingested_item WHERE published_ts <= :ts ORDER BY id"),
                             {"ts": timestamp})).all()
    return [row[0] for row in rows]
'''
CLIENT = '''import httpx

class AlpacaPaperClient:
    async def get_account(self):
        return {}
'''
TRACKER = '''from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def get_spy_benchmark(start, end):
    return 0.0
'''
BACKTESTER = '''from app.modules.paper_spy_benchmark_tracker import get_spy_benchmark
from app.modules.paper_point_time_store import get_data_as_of

async def run_backtest(start, end):
    return {"spy": await get_spy_benchmark(start, end), "trades": 0}
'''
WRITER = '''from app.database import async_session

async def write_signal(*, ingested_item_id, ticker):
    return 1

def build_row(ticker):
    def inner():
        return 5
    return {"ticker": ticker, "ok": True}
'''
MODULES = {"paper_point_time_store": STORE, "paper_alpaca_paper_client": CLIENT,
           "paper_spy_benchmark_tracker": TRACKER, "paper_walk_forward_backtester": BACKTESTER,
           "paper_audit_trail_writer": WRITER}
GENERATOR = '''# app/modules/paper_signal_generator.py
from app.modules.paper_point_time_store import get_data_as_of
from app.modules.paper_audit_trail_writer import write_signal
from app.modules.paper_walk_forward_backtester import run_backtest
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient

async def generate_signals(timestamp, *, db=None):
    return [dict(i) for i in await get_data_as_of(timestamp, db=db)]

def _own_helper():
    return 1
'''


def _files(test: str) -> dict[str, str]:
    return {"app/modules/paper_signal_generator.py": GENERATOR, "tests/test_paper_signal_generator.py": test}


def test_a_fake_of_a_module_that_only_reads_the_database_is_refused_with_what_it_really_returns():
    test = '''import pytest

async def _fake(timestamp, *, db=None):
    return [{"id": 1, "payload": {}}]

async def test_x(monkeypatch):
    monkeypatch.setattr("app.modules.paper_signal_generator.get_data_as_of", _fake)
'''
    found = internal_fakes(_files(test), MODULES, {"paper_signal_generator"})
    assert found == [
        "`tests/test_paper_signal_generator.py:7` replaces `get_data_as_of` (`paper_point_time_store`, a finished "
        "module that never leaves the engine) with a stand-in: call the real one -- against the scratch database, with "
        "the rows it reads, when it uses one -- so the test sees what it really does (it returns `[row[0] for row in "
        "rows]` of `SELECT payload FROM ingested_item WHERE published_ts <= :ts ORDER BY id`). Mock only what reaches "
        "outside the engine -- the broker, the network, model calls, the clock"]


def test_every_way_of_naming_the_target_is_read_and_a_dict_returner_names_its_keys():
    test = '''from unittest.mock import patch
from app.modules import paper_signal_generator as sg
import app.modules.paper_audit_trail_writer as w

def test_a():
    with patch("app.modules.paper_audit_trail_writer.build_row"):
        pass

def test_b(monkeypatch):
    monkeypatch.setattr(sg, "write_signal", lambda **k: 1)

def test_c():
    with patch.object(w, "build_row"):
        pass
'''
    found = internal_fakes(_files(test), MODULES, {"paper_signal_generator"})
    assert [f.split(" with a stand-in")[0] for f in found] == [
        "`tests/test_paper_signal_generator.py:6` replaces `build_row` (`paper_audit_trail_writer`, a finished module "
        "that never leaves the engine)",
        "`tests/test_paper_signal_generator.py:10` replaces `write_signal` (`paper_audit_trail_writer`, a finished "
        "module that never leaves the engine)"]                                      # build_row once, not twice
    assert "(it returns dicts with keys `ok`, `ticker`)" in found[0]                 # not the nested `return 5`


def test_what_reaches_outside_and_the_steps_own_code_may_be_mocked():
    test = '''from unittest.mock import patch

def test_a(monkeypatch):
    monkeypatch.setattr("app.modules.paper_signal_generator.AlpacaPaperClient", object)
    monkeypatch.setattr("app.modules.paper_signal_generator.run_backtest", object)
    monkeypatch.setattr("app.modules.paper_signal_generator._own_helper", object)
    monkeypatch.setattr("app.modules.paper_signal_generator.async_session", object)
    monkeypatch.setattr("datetime.datetime", object)
'''
    assert internal_fakes(_files(test), MODULES, {"paper_signal_generator"}) == []
    # a step rewriting an earlier module in place finds that module among the finished ones too (T6U6, T1F4's
    # generator): its own helpers stay its own
    earlier = {**MODULES, "paper_signal_generator": "from app.modules.paper_point_time_store import get_data_as_of\n\n"
                                                    "def _own_helper():\n    return 1\n"}
    assert internal_fakes(_files(test), earlier, {"paper_signal_generator"}) == []
    job = set(MODULES)
    assert reaches_outside(CLIENT, job) and not reaches_outside(STORE, job)
    assert not reaches_outside(BACKTESTER, job)                                   # alone, it reaches nothing
    assert reaches_outside(BACKTESTER, job, MODULES)                              # through the tracker, the broker
    assert reaches_outside("from app.model_router import route\n", job)           # a model
    assert not reaches_outside("from app.config import settings\n", job)


@pytest.mark.integration
async def test_the_engines_findings_refuse_the_stand_in():
    import json
    import uuid

    from app.database import async_session
    from app.modules import engine_source, node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    brief = {"title": "lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}
    fake = ('```python\n# tests/test_paper_signal_generator.py\nasync def test_x(monkeypatch):\n'
            '    monkeypatch.setattr("app.modules.paper_signal_generator.get_data_as_of", None)\n'
            '    await db.execute(text("DELETE FROM ingested_item"))\n```')
    migration = ('```python\n# alembic/versions/0010_lab.py\ndef upgrade():\n'
                 '    op.execute("CREATE TABLE ingested_item (id serial PRIMARY KEY, payload json)")\n'
                 '    for table in ("ingested_item",):\n'
                 '        op.execute(f"CREATE TRIGGER t BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW '
                 'EXECUTE FUNCTION forbid_mutation();")\n```')
    binds = ("```python\n# app/modules/paper_signal_saver.py\nfrom sqlalchemy import text\n\nasync def save(db, item):\n"
             "    await db.execute(text(\"INSERT INTO signal (payload) VALUES (:payload)\"), {\"payload\": {\"a\": 1}})\n```")
    output = f"```python\n{GENERATOR}```\n\n{fake}\n\n{binds}"
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        for i, (k, st, prompt, out) in enumerate([
                ("T13", "done", "Python module `paper_point_time_store.py`.",
                 f"```python\n# app/modules/paper_point_time_store.py\n{STORE}```"),
                ("T3", "done", "The audit-trail migration.", migration),
                ("T6U6", "running", "Python module `paper_signal_generator.py`.", None)]):
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
                "execution_order) VALUES (:j, :k, :k, :s, :p, :o, 'CodeGen', :n)"),
                {"j": jid, "k": k, "s": st, "p": prompt, "o": out, "n": i})
        await db.commit()
    try:
        async with async_session() as db:
            found = await engine_source.engine_findings(db, jid, "T6U6", output)
        assert any(f.startswith("`tests/test_paper_signal_generator.py:3` replaces `get_data_as_of` "
                                "(`paper_point_time_store`") for f in found), found
        assert any(f.startswith("`tests/test_paper_signal_generator.py:4` deletes from `ingested_item`, an "
                                "append-only table") for f in found), found                      # §17.1841
        assert any(f.startswith("`app/modules/paper_signal_saver.py:5` binds a dict to `:payload`") for f in found), \
            found                                                                                     # §17.1843
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


# ── §17.1839 — the names through which the job's modules leave the engine, as patch targets ───────────────────

def test_a_code_step_is_shown_exactly_what_it_may_mock():
    """Live (5b4880c0 T6): told to mock only model calls, the batch could not see that `propose_trades` calls the
    model through `generate_json`; three of four attempts stood in for internal modules instead."""
    from app.modules.engine_contracts import outside_block, outside_points
    proposer = ("from app.model_router import generate_json\nfrom app.modules.paper_risk_control_module import "
                "evaluate_proposal\n\nasync def propose_trades(signals):\n    return []\n")
    client = "import httpx\nfrom app.modules.runner_secrets import values_for\n\nclass AlpacaPaperClient:\n    pass\n"
    mods = {**MODULES, "paper_trade_proposer": proposer, "paper_alpaca_paper_client": client,
            "paper_risk_control_module": "def evaluate_proposal(**k):\n    return True\n"}
    assert outside_points(mods) == [
        "- `paper_alpaca_paper_client`: `app.modules.paper_alpaca_paper_client.httpx` (the network), "
        "`app.modules.paper_alpaca_paper_client.values_for` (a secret)",
        "- `paper_trade_proposer`: `app.modules.paper_trade_proposer.generate_json` (a model call)"]
    block = outside_block(mods)
    assert block.startswith("\n\n## Where this job's modules leave the engine (§17.1839)\nA test mocks these names")
    assert outside_block({"paper_point_time_store": STORE}) == ""                    # nothing leaves: no block
