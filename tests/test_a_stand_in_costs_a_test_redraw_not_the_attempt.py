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
                                          stand_in_findings, findings_in_tests, writers_for)

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
    assert left == fakes and len(asked) == 1               # §17.1877 the identical file: asking again gets the same
    # a redraw that brings a finding of another kind stops the loop with that finding
    other = ["`app/modules/paper_daily_signal_proposal_batch.py:9` binds a dict to `:payload`"]   # §17.1859: module-side
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
    ask_src = src.split("async def _ask_tests(_path: str, _refusal: str, _current: str,", 1)[1].split("async def _check_tests", 1)[0]
    assert '{"role": "assistant", "content": _current}' in ask_src and '"content": output' not in ask_src


# §17.1859 — every test-file finding costs a redraw. Live (5b4880c0 T5, kimi round, 2026-10-29): attempt 2's seven
# findings were five stand-ins and two patches of names the modules lack, the module clean; attempt 3's one finding
# was a test running the order executor into the broker's secrets. Each spent the attempt.
LIVE_MIXED = [
    "`tests/test_paper_twelve_modules.py:534` replaces `load_batch_signals` (`paper_daily_signal_proposal_batch_data`, a "
    "finished module that never leaves the engine) with a stand-in: call the real one",
    "`tests/test_paper_twelve_modules.py:627` patches `execute_order` on `paper_operator_approval_gate`, which has no "
    "such name (it has logger, APPROVAL_MODULE, request_approval, handle_approval_answer; it imports logging)",
    "`tests/test_paper_twelve_modules.py:572` runs `execute_order` (`paper_order_executor`) for real, and it reads the "
    "operator secrets `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` through `values_for` (`paper_alpaca_paper_client`)",
    "`tests/test_paper_twelve_modules.py:5` writes `signal` through `write_signal(…)` with a made-up id: "
    "`signal.ingested_item_id`=1 references `ingested_item.id`",
    "`tests/test_paper_twelve_modules.py:4` deletes from `ingested_item`, an append-only table: with rows in it the "
    "DELETE fails",
    "`tests/test_paper_twelve_modules.py:9` binds a dict to `:payload` through `text()`: asyncpg takes JSON only as text",
    "`tests/test_paper_twelve_modules.py:12` reads `prices` from what `get_spy_benchmark` (`paper_spy_benchmark_tracker`) "
    "returns, and it returns dicts with keys `end_date` -- never `prices`",
]
MODULE_KIND = [
    "`handle_approval_answer(…)`: `answer` is `dict`; it was given a `str` -- the signature is `async "
    "handle_approval_answer(job_id, node_key, answer: dict, payload)` (`paper_operator_approval_gate`, step f00ea97f:T16)",
    "`app/modules/paper_execution_entry_point.py:208` reads `operator_id` from what `request_approval` returns",
    "`SELECT … ticker … FROM fill`: `fill` has no column `ticker`",
]


def test_every_kind_of_test_file_finding_is_one_and_a_module_finding_is_not():
    assert findings_in_tests(LIVE_MIXED) == LIVE_MIXED
    assert findings_in_tests(MODULE_KIND) == []
    assert findings_in_tests(LIVE_MIXED + MODULE_KIND) == LIVE_MIXED
    assert stand_in_findings(LIVE_MIXED) == LIVE_MIXED[:1]                        # §17.1852's narrower set stands


@pytest.mark.asyncio
async def test_a_mix_of_test_file_findings_costs_a_redraw_and_a_module_finding_still_stops_it():
    asked: list[str] = []

    async def ask(path, refusal, current):
        asked.append(refusal)
        return f"```python\n{REAL}```"

    async def clean(output):
        return [] if REAL in output else LIVE_MIXED

    out, left = await redraw_faked_tests(OUT, LIVE_MIXED, ask=ask, check=clean)
    assert left == [] and REAL in out and len(asked) == 1
    assert asked[0] == "\n".join(f"  - {f}" for f in LIVE_MIXED)
    # one finding, not a stand-in (live attempt 3): still a redraw, not the attempt
    out, left = await redraw_faked_tests(OUT, LIVE_MIXED[2:3], ask=ask, check=clean)
    assert left == [] and len(asked) == 2
    # a module-kind finding among them: the ordinary refusal, nothing asked
    asked.clear()
    out, left = await redraw_faked_tests(OUT, LIVE_MIXED + MODULE_KIND[:1], ask=ask, check=clean)
    assert (out, left) == (OUT, LIVE_MIXED + MODULE_KIND[:1]) and asked == []
    # and the executor gates on the wide set
    import inspect

    from app.modules import execution_agent
    src = inspect.getsource(execution_agent)
    assert "if _refs and _ec.findings_in_tests(_refs) == _refs:" in src
    assert "_ec.stand_in_findings(_refs) == _refs" not in src


# §17.1861 — a call-shape finding names its file and line, so in a test file it is a test-file finding. Live
# (5b4880c0 T5, 2026-10-29): attempt 2 was spent on two `handle_approval_answer(…)`: `answer` is `dict`; it was given
# a `str` -- both in the test file, neither saying so, so the §17.1859 redraw could not take them.
GATE_ROW = {"node_key": "T16", "status": "done", "title": "gate", "prompt_template": "", "output_text":
            "```python\n# app/modules/paper_operator_approval_gate.py\n"
            "async def handle_approval_answer(job_id, node_key, answer: dict, payload):\n    return {}\n```"}


def test_a_call_shape_finding_in_a_test_file_names_it_and_counts_as_a_test_file_finding():
    from app.modules import engine_source as es
    defs = es.sibling_definitions([GATE_ROW])
    test = ('```python\n# tests/test_paper_twelve_modules.py\n'
            'from app.modules.paper_operator_approval_gate import handle_approval_answer\n\n'
            'async def test_x():\n    await handle_approval_answer("j", "k", "yes", {})\n```')
    found = es.sibling_refs(test, defs)
    assert found == ["`tests/test_paper_twelve_modules.py:5` calls `handle_approval_answer(…)`: `answer` is `dict`; it "
                     "was given a `str` -- the signature is `async handle_approval_answer(job_id, node_key, answer: dict, "
                     "payload)` (`paper_operator_approval_gate`, step T16)"]
    assert findings_in_tests(found) == found
    # the step's own module: named too, but not a test-file finding; a block with no path: as before
    mod = test.replace("# tests/test_paper_twelve_modules.py", "# app/modules/paper_runner.py")
    assert es.sibling_refs(mod, defs)[0].startswith("`app/modules/paper_runner.py:5` calls `handle_approval_answer(…)`")
    assert findings_in_tests(es.sibling_refs(mod, defs)) == []
    bare = test.replace("# tests/test_paper_twelve_modules.py\n", "")
    assert es.sibling_refs(bare, defs)[0].startswith("`handle_approval_answer(…)`: `answer` is `dict`")
    # the engine's own functions, the same way (the §17.1761 live shape, in a test file)
    eng = ('```python\n# tests/test_paper_x.py\nfrom app.modules.decision_pause import frame_decision\n\n'
           'async def test_y(node):\n    await frame_decision(node, brief="b", upstream={"proposal": 1})\n```')
    hit = [f for f in es.unresolved_refs(eng) if "`upstream` is `str`" in f]
    assert hit and hit[0].startswith("`tests/test_paper_x.py:5` calls `frame_decision(…)`: `upstream` is `str`; it was given a `dict`")
    assert findings_in_tests(hit) == hit


async def _aret(v):
    return v
