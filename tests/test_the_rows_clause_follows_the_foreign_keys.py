"""§17.1853 — the rows clause follows the job's foreign keys: parent rows first, each with its writer or an INSERT.

Live (follow-up 5b4880c0 T6, 2026-10-28, the attempt after the §17.1852 redraw): the test called exactly the writer
the refusal named, `write_order(approval_id=1, proposal_id=1, broker_order_id="…")`, and the scratch database refused
the row -- "Key (approval_id)=(1) is not present in table approval". `approval` needs a `proposal`, `proposal` a
`signal`, `signal` an `ingested_item`, and every finished writer of `ingested_item` is an ingester that fetches from
the network, so a test fills it with an INSERT. The clause named the table and its writer, never the parent rows the
writer needs; the sandbox did not recognise the violation either. Now the clause walks the job's foreign keys
(§17.1789 reads them from its migration) and lists the tables parents first, each with its finished writer or an
INSERT where none stays inside the engine; and the sandbox names each missing parent next to the error.
"""
from __future__ import annotations

import pytest

from app.modules.engine_contracts import internal_fakes, rows_clause, seed_order
from app.sandbox import engine_tests as et

WRITER = '''from sqlalchemy import text

async def write_signal(*, db, ingested_item_id, ticker):
    return (await db.execute(text("INSERT INTO signal (ingested_item_id, ticker) VALUES (:i, :t) RETURNING id"),
                             {"i": ingested_item_id, "t": ticker})).scalar_one()


async def write_proposal(*, db, signal_id, ticker):
    return (await db.execute(text("INSERT INTO proposal (signal_id, ticker) VALUES (:s, :t) RETURNING id"),
                             {"s": signal_id, "t": ticker})).scalar_one()


async def write_approval(*, db, proposal_id, decision):
    return (await db.execute(text("INSERT INTO approval (proposal_id, decision) VALUES (:p, :d) RETURNING id"),
                             {"p": proposal_id, "d": decision})).scalar_one()


async def write_order(*, db, approval_id, proposal_id, broker_order_id):
    return (await db.execute(text("INSERT INTO broker_order_id (approval_id, proposal_id, broker_order_id) "
                                  "VALUES (:a, :p, :b) RETURNING id"),
                             {"a": approval_id, "p": proposal_id, "b": broker_order_id})).scalar_one()


async def write_pnl(*, db, as_of_ts, equity):
    return (await db.execute(text("INSERT INTO pnl (as_of_ts, equity) VALUES (:ts, :eq) RETURNING id"),
                             {"ts": as_of_ts, "eq": equity})).scalar_one()
'''
NEWS = '''import httpx
from sqlalchemy import text

async def store_news_item(*, db, item):
    return (await db.execute(text("INSERT INTO ingested_item (source, payload) VALUES ('news', :p) RETURNING id"),
                             {"p": item})).scalar_one()
'''
DATA = '''from sqlalchemy import text

async def load_batch_context(*, trading_day, db=None):
    equity = (await db.execute(text("SELECT equity FROM pnl WHERE as_of_ts >= :d ORDER BY as_of_ts LIMIT 1"),
                               {"d": trading_day})).scalar()
    n = (await db.execute(text("SELECT count(*) FROM broker_order_id WHERE created_at >= :d"), {"d": trading_day})).scalar()
    return {"day_start_equity": equity, "orders_today": int(n or 0)}


async def load_batch_signals(*, db=None):
    rows = (await db.execute(text("SELECT s.ticker, i.payload FROM signal s JOIN ingested_item i ON i.id = s.ingested_item_id"))).all()
    return [dict(r._mapping) for r in rows]
'''
MODULES = {"paper_audit_trail_writer": WRITER, "paper_news_ingestion": NEWS,
           "paper_daily_signal_proposal_batch_data": DATA}
FKS = {"broker_order_id": {"approval_id": "approval.id", "proposal_id": "proposal.id"},
       "approval": {"proposal_id": "proposal.id"}, "proposal": {"signal_id": "signal.id"},
       "signal": {"ingested_item_id": "ingested_item.id"}, "fill": {"broker_order_id": "broker_order_id.id"},
       "risk_check": {"proposal_id": "proposal.id"}}
BATCH = '''# app/modules/paper_daily_signal_proposal_batch.py
from app.modules.paper_daily_signal_proposal_batch_data import load_batch_context, load_batch_signals
'''
FAKED = '''# tests/test_paper_daily_signal_proposal_batch.py
from unittest.mock import AsyncMock, patch

async def test_x():
    with patch("app.modules.paper_daily_signal_proposal_batch.load_batch_context", new=AsyncMock(return_value={})):
        pass
    with patch("app.modules.paper_daily_signal_proposal_batch.load_batch_signals", new=AsyncMock(return_value=[])):
        pass
'''
FILES = {"tests/test_paper_daily_signal_proposal_batch.py": FAKED, "app/modules/paper_daily_signal_proposal_batch.py": BATCH}
OWN = {"paper_daily_signal_proposal_batch"}
EDGES = ["broker_order_id.approval_id → approval.id", "broker_order_id.proposal_id → proposal.id",
         "approval.proposal_id → proposal.id", "proposal.signal_id → signal.id",
         "signal.ingested_item_id → ingested_item.id"]


def test_the_seed_order_puts_every_parent_before_its_child():
    assert seed_order(FKS, {"broker_order_id", "pnl"}) == (
        ["ingested_item", "pnl", "signal", "proposal", "approval", "broker_order_id"], EDGES)
    assert seed_order(FKS, {"ingested_item", "signal"}) == (["ingested_item", "signal"],
                                                            ["signal.ingested_item_id → ingested_item.id"])
    assert seed_order({}, {"pnl", "approval"}) == (["approval", "pnl"], [])             # no keys: name order
    assert seed_order({"node": {"parent_id": "node.id"}}, {"node"}) == (["node"], [])   # a self-reference
    loop = {"a": {"b_id": "b.id"}, "b": {"a_id": "a.id"}}
    assert seed_order(loop, {"a"}) == (["a", "b"], ["a.b_id → b.id", "b.a_id → a.id"])  # a cycle still ends
    chain = {f"t{i}": {"p": f"t{i + 1}.id"} for i in range(9)}
    order, edges = seed_order(chain, {"t0"}, hops=2)
    assert order == ["t2", "t1", "t0"] and len(edges) == 2                               # bounded


def test_the_clause_lists_the_parents_first_each_with_its_writer_or_an_insert():
    found = internal_fakes(FILES, MODULES, OWN, fks=FKS)
    assert len(found) == 2
    assert found[0] == (
        "`tests/test_paper_daily_signal_proposal_batch.py:5` replaces `load_batch_context` "
        "(`paper_daily_signal_proposal_batch_data`, a finished module that never leaves the engine) with a stand-in: "
        "call the real one -- against the scratch database, with the rows it reads, when it uses one -- so the test "
        "sees what it really does (it returns dicts with keys `day_start_equity`, `orders_today`) -- it reads "
        "`broker_order_id`, `pnl`; the scratch database enforces " + ", ".join(EDGES) + ", so insert in this order: "
        "`ingested_item` by an INSERT through `async_session` + `text` (no finished writer stays inside the engine; "
        "its columns are in this job's tables above); `pnl` with `write_pnl(*, db, as_of_ts, equity)` "
        "(`paper_audit_trail_writer`); `signal` with `write_signal(*, db, ingested_item_id, ticker)` "
        "(`paper_audit_trail_writer`); `proposal` with `write_proposal(*, db, signal_id, ticker)` "
        "(`paper_audit_trail_writer`); `approval` with `write_approval(*, db, proposal_id, decision)` "
        "(`paper_audit_trail_writer`); `broker_order_id` with `write_order(*, db, approval_id, proposal_id, "
        "broker_order_id)` (`paper_audit_trail_writer`) -- each child row takes the id its parent's writer returned, "
        "never a made-up one. Mock only what reaches outside the engine -- the broker, the network, model calls, the "
        "clock")
    # the signals loader reads the child and its parent: the parent first, and the network ingester is not its writer
    assert ("-- it reads `ingested_item`, `signal`; the scratch database enforces signal.ingested_item_id → "
            "ingested_item.id, so insert in this order: `ingested_item` by an INSERT through `async_session` + `text` "
            "(no finished writer stays inside the engine; its columns are in this job's tables above); `signal` with "
            "`write_signal(*, db, ingested_item_id, ticker)` (`paper_audit_trail_writer`) -- each child row takes the "
            "id its parent's writer returned, never a made-up one. Mock only") in found[1]
    # without the job's foreign keys the §17.1852 clause stands as it was
    flat = internal_fakes(FILES, MODULES, OWN)
    assert "-- it reads `broker_order_id`, `pnl`: insert those rows first, with `write_order(*, db, approval_id, " \
           "proposal_id, broker_order_id)` (`paper_audit_trail_writer`), `write_pnl(*, db, as_of_ts, equity)` " \
           "(`paper_audit_trail_writer`). Mock only" in flat[0]
    assert rows_clause(MODULES, set(), FKS) == ""
    assert rows_clause(MODULES, {"pnl"}, FKS) == \
        " -- it reads `pnl`: insert those rows first, with `write_pnl(*, db, as_of_ts, equity)` (`paper_audit_trail_writer`)"


# the live sandbox output, attempt 2 (2026-10-28 03:08 UTC), pytest --tb=short
LIVE_FK = (
    "______________ test_run_daily_batch_uses_real_load_batch_context _______________\n"
    "  … 5 library frames …\n"
    'E   asyncpg.exceptions.ForeignKeyViolationError: insert or update on table "broker_order_id" violates foreign key '
    'constraint "broker_order_id_approval_id_fkey"\n'
    'E   DETAIL:  Key (approval_id)=(1) is not present in table "approval".\n'
    "The above exception was the direct cause of the following exception:\n"
    "tests/test_paper_daily_signal_proposal_batch.py:322: in test_run_daily_batch_uses_real_load_batch_context\n"
    "    await write_order(\n"
    "app/modules/paper_audit_trail_writer.py:177: in write_order\n"
    "    result = await db.execute(\n"
    'E   sqlalchemy.exc.IntegrityError: (sqlalchemy.dialects.postgresql.asyncpg.ForeignKeyViolationError) insert or '
    'update on table "broker_order_id" violates foreign key constraint "broker_order_id_approval_id_fkey"\n'
    'E   DETAIL:  Key (approval_id)=(1) is not present in table "approval".\n'
    "2 failed, 3 passed in 2.4s")


def test_the_sandbox_names_the_missing_parent_row_next_to_the_error():
    cause = et.known_causes(LIVE_FK)
    assert cause.count("Cause the engine recognises") == 1
    assert "insert the parent row first" in cause and "never a made-up one" in cause
    assert cause.endswith("; this run: `approval_id`=1 has no `approval` row.")              # once, not twice
    assert "parent row" not in et.known_causes("E   assert 1 == 2\n1 failed")
    assert "parent row" not in et.known_causes(LIVE_FK.replace("violates foreign key", "violates check"))
    assert "this run:" not in et.known_causes('E   violates foreign key constraint "x"\n1 failed')   # no DETAIL


@pytest.mark.integration
async def test_the_engines_findings_read_the_foreign_keys_from_the_jobs_migration():
    """The wiring: `engine_findings` hands §17.1789's foreign keys (read from the job's migration) to the clause."""
    import json
    import uuid

    from app.database import async_session
    from app.modules import engine_source, node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    brief = {"title": "lab", "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}
    migration = ('```python\n# alembic/versions/0010_lab.py\ndef upgrade():\n'
                 '    op.execute("CREATE TABLE ingested_item (id serial PRIMARY KEY, payload json)")\n'
                 '    op.execute("CREATE TABLE signal (id serial PRIMARY KEY, ingested_item_id integer NOT NULL '
                 'REFERENCES ingested_item(id), ticker text)")\n```')
    output = f"```python\n{BATCH}```\n\n```python\n{FAKED}```"
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        for i, (k, st, prompt, out) in enumerate([
                ("T3", "done", "The audit-trail migration.", migration),
                ("T4", "done", "Python module `paper_audit_trail_writer.py`.",
                 f"```python\n# app/modules/paper_audit_trail_writer.py\n{WRITER}```"),
                ("T5", "done", "Python module `paper_daily_signal_proposal_batch_data.py`.",
                 f"```python\n# app/modules/paper_daily_signal_proposal_batch_data.py\n{DATA}```"),
                ("T6", "running", "Python module `paper_daily_signal_proposal_batch.py`.", None)]):
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, output_text, tool, "
                "execution_order) VALUES (:j, :k, :k, :s, :p, :o, 'CodeGen', :n)"),
                {"j": jid, "k": k, "s": st, "p": prompt, "o": out, "n": i})
        await db.commit()
    try:
        async with async_session() as db:
            found = await engine_source.engine_findings(db, jid, "T6", output)
        hit = [f for f in found if "replaces `load_batch_signals`" in f]
        assert len(hit) == 1, found
        assert ("the scratch database enforces signal.ingested_item_id → ingested_item.id, so insert in this order: "
                "`ingested_item` by an INSERT through `async_session` + `text` (no finished writer stays inside the "
                "engine; its columns are in this job's tables above); `signal` with `write_signal(*, db, "
                "ingested_item_id, ticker)` (`paper_audit_trail_writer`)") in hit[0], hit[0]
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
