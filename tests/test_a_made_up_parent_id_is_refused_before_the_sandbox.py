"""§17.1858 — a test that writes a child row with a made-up id for a foreign key is refused before the sandbox; and
the sandbox names a column written null next to the error.

Live (follow-up 5b4880c0, 2026-10-28): after §17.1853 had said "never a made-up one", T6's attempt wrote
`write_order(approval_id=1, …)` and T5's wrote `write_signal(ingested_item_id=1, …)` twice; the scratch database
refused each ("Key (ingested_item_id)=(1) is not present in table ingested_item") and the attempt was spent. T5's
approval tests also called the gate without a proposal: `null value in column "proposal_id" of relation "approval"`.
"""
from __future__ import annotations

from app.modules.engine_contracts import made_up_parent_ids
from app.sandbox import engine_tests as et

WRITER = '''from sqlalchemy import text


async def write_signal(*, db, ingested_item_id, ticker):
    return (await db.execute(text("INSERT INTO signal (ingested_item_id, ticker) VALUES (:i, :t) RETURNING id"),
                             {"i": ingested_item_id, "t": ticker})).scalar_one()


async def write_proposal(*, db, signal_id, ticker):
    return (await db.execute(text("INSERT INTO proposal (signal_id, ticker) VALUES (:s, :t) RETURNING id"),
                             {"s": signal_id, "t": ticker})).scalar_one()


async def write_pnl(*, db, equity):
    return (await db.execute(text("INSERT INTO pnl (equity) VALUES (:e) RETURNING id"), {"e": equity})).scalar_one()
'''
MODULES = {"paper_audit_trail_writer": WRITER, "paper_news_ingestion": "import httpx\n"}
FKS = {"signal": {"ingested_item_id": "ingested_item.id"}, "proposal": {"signal_id": "signal.id"}}
BATCH = "# app/modules/paper_tests_runner.py\nX = 1\n"


def _files(test: str) -> dict[str, str]:
    return {"app/modules/paper_tests_runner.py": BATCH, "tests/test_paper_twelve_modules.py": test}


def test_the_live_made_up_ids_are_refused_with_the_parents_to_insert_first():
    test = '''from sqlalchemy import text
from app.modules.paper_audit_trail_writer import write_signal, write_proposal, write_pnl

async def test_x(db):
    sid = await write_signal(db=db, ingested_item_id=1, ticker="SPY")
    pid = await write_proposal(db=db, signal_id=sid, ticker="SPY")                 # the returned id: fine
    await write_pnl(db=db, equity=1)                                                # no foreign key: fine
    await db.execute(text("INSERT INTO proposal (signal_id, ticker) VALUES (7, 'SPY')"))
    await db.execute(text("INSERT INTO proposal (signal_id, ticker) VALUES (:s, 'SPY')"), {"s": sid})
'''
    found = made_up_parent_ids(_files(test), MODULES, FKS)
    assert found == [
        "`tests/test_paper_twelve_modules.py:5` writes `signal` through `write_signal(…)` with a made-up id: "
        "`signal.ingested_item_id`=1 references `ingested_item.id`, and the scratch database refuses it (no such row) "
        "-- insert the parent rows first, in this order: `ingested_item` by an INSERT through `async_session` + `text` "
        "(no finished writer stays inside the engine; its columns are in this job's tables above) -- and pass the id "
        "each writer returned",
        "`tests/test_paper_twelve_modules.py:8` writes `proposal` with its own INSERT with a made-up id: "
        "`proposal.signal_id`=7 references `signal.id`, and the scratch database refuses it (no such row) -- insert the "
        "parent rows first, in this order: `ingested_item` by an INSERT through `async_session` + `text` (no finished "
        "writer stays inside the engine; its columns are in this job's tables above); `signal` with "
        "`write_signal(*, db, ingested_item_id, ticker)` (`paper_audit_trail_writer`) -- and pass the id each writer "
        "returned"]
    assert made_up_parent_ids(_files(test), MODULES, {}) == []                              # no keys known: nothing
    assert made_up_parent_ids(_files("x = 1\n"), MODULES, FKS) == []


LIVE_NULL = ('E   sqlalchemy.exc.IntegrityError: (sqlalchemy.dialects.postgresql.asyncpg.NotNullViolationError) null value '
             'in column "proposal_id" of relation "approval" violates not-null constraint\n4 failed, 11 passed in 8.02s')


def test_the_sandbox_names_the_column_written_null():
    cause = et.known_causes(LIVE_NULL)
    assert cause.count("Cause the engine recognises") == 1
    assert "has no default" in cause and cause.endswith("; this run: `approval.proposal_id` was null.")
    assert "default" not in et.known_causes("E   assert None is not None\n1 failed")
