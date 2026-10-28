"""§17.1852 — a stand-in in the tests costs a test redraw, not the attempt; and the refusal names the rows.

Live (follow-up 5b4880c0 T6, 2026-10-28, four rounds on two models): three of four attempts were refused for stand-ins
in the test file and nothing else -- the module had passed every check -- and each cost the whole attempt, which then
regenerated the module too. The refusal said "insert the rows it reads" without saying which rows. Now: the refusal
names the tables the real function reads (followed through the job's modules) and the finished writers that insert
them; and when every finding is such a stand-in, the test file is asked for again, alone, with the module in hand and
checked again, up to twice, before a retry is spent.
"""
from __future__ import annotations

import pytest

from app.modules.engine_contracts import (internal_fakes, reads_tables, redraw_faked_tests, replace_file_block,
                                          stand_in_findings, writers_for)

STORE = '''from sqlalchemy import text

async def get_data_as_of(timestamp, *, db=None):
    rows = (await db.execute(text("SELECT payload FROM ingested_item WHERE published_ts <= :ts ORDER BY id"),
                             {"ts": timestamp})).all()
    return [row[0] for row in rows]


async def write_ingested_item(*, db, source, external_id, payload):
    """Save one item."""
    return (await db.execute(text("INSERT INTO ingested_item (source, external_id, payload) VALUES (:s, :e, :p) "
                                  "RETURNING id"), {"s": source, "e": external_id, "p": payload})).scalar_one()
'''
EQUITY = '''from sqlalchemy import text

async def record_day_start_equity(*, db, equity, as_of_ts=None):
    sql = text("INSERT INTO pnl (as_of_ts, equity) VALUES (:ts, :eq) RETURNING id")
    return (await db.execute(sql, {"ts": as_of_ts, "eq": equity})).scalar_one()


async def get_day_start_equity(*, db, trading_day):
    """Read the day-start equity from the pnl audit trail (never from an order, join nothing)."""
    row = (await db.execute(text("SELECT equity FROM pnl WHERE as_of_ts >= :d ORDER BY as_of_ts LIMIT 1"),
                            {"d": trading_day})).first()
    return row[0] if row else None
'''
GENERATOR = '''from app.modules.paper_point_time_store import get_data_as_of

async def generate_signals(timestamp, *, db=None):
    items = await get_data_as_of(timestamp, db=db)
    return [{"ticker": i.get("ticker"), "signal_id": 1} for i in items]
'''
DATA = '''from sqlalchemy import text
from app.modules.paper_day_start_equity_recording import get_day_start_equity
from app.modules.paper_signal_generator import generate_signals


async def load_batch_context(*, timestamp, trading_day, db=None):
    day_start_equity = await get_day_start_equity(db=db, trading_day=trading_day)
    n = (await db.execute(text("WITH today AS (SELECT 1) SELECT count(*) FROM broker_order o JOIN today t ON true "
                               "WHERE o.created_at >= :d"), {"d": trading_day})).scalar()
    return {"day_start_equity": day_start_equity, "orders_today": int(n or 0)}


async def load_batch_signals(*, timestamp, db=None):
    return await generate_signals(timestamp, db=db)
'''
MODULES = {"paper_point_time_store": STORE, "paper_day_start_equity_recording": EQUITY,
           "paper_signal_generator": GENERATOR, "paper_daily_signal_proposal_batch_data": DATA}
BATCH = '''# app/modules/paper_daily_signal_proposal_batch.py
from app.modules.paper_daily_signal_proposal_batch_data import load_batch_context, load_batch_signals

async def run_daily_batch(*, timestamp, db=None):
    return {"proposals": 0}
'''
FAKED = '''# tests/test_paper_daily_signal_proposal_batch.py
from unittest.mock import AsyncMock, patch

async def test_x():
    with patch("app.modules.paper_daily_signal_proposal_batch.load_batch_context", new=AsyncMock(return_value={})):
        pass
    with patch("app.modules.paper_daily_signal_proposal_batch.load_batch_signals", new=AsyncMock(return_value=[])):
        pass
'''
REAL = '''# tests/test_paper_daily_signal_proposal_batch.py
from sqlalchemy import text

async def test_x(db):
    await db.execute(text("INSERT INTO ingested_item (source, external_id, payload) VALUES ('news', 'a', '{}')"))
'''
OUT = f"```python\n{BATCH}```\n\n```python\n{FAKED}```"
OWN = {"paper_daily_signal_proposal_batch"}


def test_the_tables_a_function_reads_are_followed_through_the_jobs_modules():
    assert reads_tables(MODULES, "paper_daily_signal_proposal_batch_data", "load_batch_context") == {"pnl", "broker_order"}
    assert reads_tables(MODULES, "paper_daily_signal_proposal_batch_data", "load_batch_signals") == {"ingested_item"}
    assert reads_tables(MODULES, "paper_point_time_store", "write_ingested_item") == set()       # an insert reads nothing
    assert reads_tables(MODULES, "paper_point_time_store", "nope") == set()
    assert writers_for(MODULES, {"pnl", "ingested_item"}) == [
        "`record_day_start_equity(*, db, equity, as_of_ts=…)` (`paper_day_start_equity_recording`)",
        "`write_ingested_item(*, db, source, external_id, payload)` (`paper_point_time_store`)"]
    assert writers_for(MODULES, {"broker_order"}) == []
    fetcher = "import httpx\nfrom sqlalchemy import text\n\nasync def ingest(*, db):\n    await db.execute(text(" \
              "\"INSERT INTO ingested_item (source, external_id, payload) VALUES (:s, :e, :p)\"), {})\n"
    assert writers_for({**MODULES, "paper_news_ingestion": fetcher}, {"ingested_item"}) == [
        "`write_ingested_item(*, db, source, external_id, payload)` (`paper_point_time_store`)"]   # reaches outside


def test_the_refusal_names_the_rows_and_the_writer():
    found = internal_fakes({"tests/test_paper_daily_signal_proposal_batch.py": FAKED,
                            "app/modules/paper_daily_signal_proposal_batch.py": BATCH}, MODULES, OWN)
    assert len(found) == 2
    assert found[0] == (
        "`tests/test_paper_daily_signal_proposal_batch.py:5` replaces `load_batch_context` "
        "(`paper_daily_signal_proposal_batch_data`, a finished module that never leaves the engine) with a stand-in: "
        "call the real one -- against the scratch database, with the rows it reads, when it uses one -- so the test "
        "sees what it really does (it returns dicts with keys `day_start_equity`, `orders_today`) -- it reads "
        "`broker_order`, `pnl`: insert those rows first, with `record_day_start_equity(*, db, equity, as_of_ts=…)` "
        "(`paper_day_start_equity_recording`). Mock only what reaches outside the engine -- the broker, the network, "
        "model calls, the clock")
    assert "-- it reads `ingested_item`: insert those rows first, with `write_ingested_item(*, db, source, " \
           "external_id, payload)` (`paper_point_time_store`)" in found[1]
    assert stand_in_findings(found) == found
    assert stand_in_findings(found + ["`app/modules/x.py:3` reads `equity` from what `f` returns"]) == found


def test_a_file_block_is_replaced_in_place_or_appended():
    out = replace_file_block(OUT, "tests/test_paper_daily_signal_proposal_batch.py", f"```python\n{REAL}```")
    assert BATCH in out and REAL in out and FAKED not in out and out.index(BATCH) < out.index(REAL)
    bare = replace_file_block(OUT, "tests/test_paper_daily_signal_proposal_batch.py", REAL)
    assert bare == out                                                               # bare code is fenced
    only = replace_file_block(f"```python\n{BATCH}```", "tests/test_paper_daily_signal_proposal_batch.py", REAL)
    assert only.endswith(f"```python\n{REAL.strip()}\n```") and BATCH in only        # none: appended


@pytest.mark.asyncio
async def test_a_stand_in_costs_a_redraw_and_the_loop_stops_when_clean_or_on_another_kind_of_finding():
    asked: list[tuple[str, str]] = []
    fakes = internal_fakes({"tests/test_paper_daily_signal_proposal_batch.py": FAKED,
                            "app/modules/paper_daily_signal_proposal_batch.py": BATCH}, MODULES, OWN)
    assert len(fakes) == 2

    async def ask(path, refusal, current):
        asked.append((path, refusal))
        return f"```python\n{REAL}```"

    async def check(output):
        return [] if REAL in output else fakes

    out, left = await redraw_faked_tests(OUT, fakes, ask=ask, check=check)
    assert left == [] and REAL in out and FAKED not in out and BATCH in out
    assert asked == [("tests/test_paper_daily_signal_proposal_batch.py", "\n".join(f"  - {f}" for f in fakes))]
    # still faked after every try: the latest output and its findings come back, `tries` asks spent
    asked.clear()

    async def still(path, refusal, current):
        asked.append((path, refusal))
        return f"```python\n{FAKED}```"

    out, left = await redraw_faked_tests(OUT, fakes, ask=still, check=lambda o: _aret(fakes), tries=2)
    assert left == fakes and len(asked) == 2
    # a redraw that brings a finding of another kind stops the loop with that finding
    other = ["`tests/test_paper_daily_signal_proposal_batch.py:9` binds a dict to `:payload`"]
    asked.clear()
    out, left = await redraw_faked_tests(OUT, fakes, ask=ask, check=lambda o: _aret(other), tries=2)
    assert left == other and len(asked) == 1 and REAL in out
    # not every finding a stand-in, or an empty answer: nothing asked / nothing changed
    mixed = fakes + other
    out, left = await redraw_faked_tests(OUT, mixed, ask=ask, check=check)
    assert (out, left) == (OUT, mixed)

    async def nothing(path, refusal, current):
        return ""

    out, left = await redraw_faked_tests(OUT, fakes, ask=nothing, check=check)
    assert (out, left) == (OUT, fakes)
    out, left = await redraw_faked_tests(OUT, [], ask=ask, check=check)
    assert (out, left) == (OUT, [])


# §17.1854 — the live shape (5b4880c0 T6, 2026-10-28): the first redraw fixes half, the second must start from it
HALF = '''# tests/test_paper_daily_signal_proposal_batch.py
from unittest.mock import AsyncMock, patch

async def test_x():
    with patch("app.modules.paper_daily_signal_proposal_batch.load_batch_signals", new=AsyncMock(return_value=[])):
        pass
'''


@pytest.mark.asyncio
async def test_the_second_redraw_is_shown_the_first_redraws_file_not_the_attempts():
    fakes = internal_fakes({"tests/test_paper_daily_signal_proposal_batch.py": FAKED,
                            "app/modules/paper_daily_signal_proposal_batch.py": BATCH}, MODULES, OWN)
    shown: list[str] = []
    answers = iter([f"```python\n{HALF}```", f"```python\n{REAL}```"])

    async def ask(path, refusal, current):
        shown.append(current)
        return next(answers)

    async def check(output):
        files = {"tests/test_paper_daily_signal_proposal_batch.py":
                 output.split("# tests/test_paper_daily_signal_proposal_batch.py", 1)[1].split("```", 1)[0],
                 "app/modules/paper_daily_signal_proposal_batch.py": BATCH}
        return internal_fakes(files, MODULES, OWN)

    out, left = await redraw_faked_tests(OUT, fakes, ask=ask, check=check, tries=2)
    assert left == [] and REAL in out
    assert len(shown) == 2
    assert shown[0] == OUT                                                      # the first ask: the attempt
    assert HALF.strip() in shown[1] and FAKED not in shown[1] and BATCH in shown[1]   # the second: the first redraw
    # and the executor's ask puts that candidate, not the attempt's output, in the assistant turn
    import inspect

    from app.modules import execution_agent
    src = inspect.getsource(execution_agent)
    ask_src = src.split("async def _ask_tests(_path: str, _refusal: str, _current: str)", 1)[1].split("async def _check_tests", 1)[0]
    assert '{"role": "assistant", "content": _current}' in ask_src and '"content": output' not in ask_src


async def _aret(v):
    return v
