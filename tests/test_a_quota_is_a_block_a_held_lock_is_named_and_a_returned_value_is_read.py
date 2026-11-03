"""§17.1889 — the parked items, closed: a model account's usage limit blocks the run instead of falling back to the
CPU model; the sandbox names the test that left a transaction open instead of timing out six setups; an `__aexit__`
that swallows; a returned compare and a returned literal read against the test's values; a text whose every row the
parser drops; a method of a job class checked against its signature; a value JSON cannot carry blamed on the module
that built the payload, not on the engine frame below it.

Live (follow-up 5b4880c0 T5): 2026-10-31 12:49 UTC three 429s "you have reached your weekly usage limit", then
"Falling back from deepseek-v4-pro:cloud → qwen3.5:latest" on the CPU for 30 minutes until the operator cancelled;
2026-10-31 a4 six "ERROR at setup of … Timeout (>20.0s)" in `_enginetest_job_tables_empty` (TRUNCATE waiting on a lock
a leaked session held); `mock_session.return_value.__aexit__ = AsyncMock()` -> "DID NOT RAISE" four rounds running;
2026-11-02 21:01–21:23 UTC (the §17.1888 image): all four attempts at the sandbox with zero findings -- `assert
result["metrics"] in ({}, None)`, `result["orders"] == 0` on a list, FINRA rows of one and three fields, `submit_order`
without `order_type`, a `Decimal` quantity refused by `json.dumps` inside the engine's `open_decision_job`."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app import model_router
from app.modules import engine_source as es
from app.modules import execution_agent as ea
from app.modules.engine_contracts import narrow_rows, returned_compares, swallowing_exits
from app.sandbox import engine_tests as et

PATH = "tests/test_paper_twelve_modules.py"

ALPACA = '''# app/modules/paper_alpaca_paper_client.py
from decimal import Decimal
from typing import Literal

class AlpacaPaperClientError(Exception):
    pass

class AlpacaPaperClient:
    def __init__(self, *, base_url: str = "https://paper") -> None:
        self._base_url = base_url

    async def submit_order(self, *, symbol: str, qty: Decimal | str, side: Literal["buy", "sell"],
                           order_type: Literal["market", "limit"], time_in_force: str = "day") -> dict:
        return {"symbol": symbol}
'''

TRACKER = '''# app/modules/paper_fill_tracker.py
from typing import Any

TRADE_UPDATES_STREAM: str = "trade_updates"

def is_trade_update(envelope: dict[str, Any]) -> bool:
    """Return True if the envelope is a trade_updates stream message."""
    return envelope.get("stream") == TRADE_UPDATES_STREAM
'''

VIEW = '''# app/modules/paper_operator_view.py
from typing import Any, Optional
from sqlalchemy import text
from app.database import async_session

async def _build(db) -> dict[str, Any]:
    metrics_row = (await db.execute(text("SELECT 1 FROM pnl LIMIT 1"))).fetchone()
    if metrics_row is not None:
        metrics = dict(metrics_row._mapping)
    else:
        metrics = {"cumulative_return": None, "max_drawdown": None}
    orders = [dict(r._mapping) for r in (await db.execute(text("SELECT 1 FROM orders"))).fetchall()]
    return {"metrics": metrics, "orders": orders}

async def get_operator_view(*, db: Optional[Any] = None) -> dict[str, Any]:
    if db is not None:
        return await _build(db)
    async with async_session() as session:
        return await _build(session)
'''

FINRA = '''# app/modules/paper_finra_data_ingestion.py
import csv
import io

def _parse_short_interest(content: str):
    reader = csv.reader(io.StringIO(content), delimiter="|")
    for line_no, row in enumerate(reader, start=1):
        if len(row) < 5:
            continue
        settlement_date, symbol, short_interest, avg_daily_volume, days_to_cover = row[:5]
        yield {"symbol": symbol}

async def ingest_finra_file(*, source: str, http_get) -> dict:
    response = await http_get("https://x")
    return {"ingested_items": len(list(_parse_short_interest(response.text)))}
'''

MODULES = {"paper_alpaca_paper_client": ALPACA, "paper_fill_tracker": TRACKER, "paper_operator_view": VIEW,
           "paper_finra_data_ingestion": FINRA}


def _row(key: str, code: str) -> dict:
    mod = code.split("\n", 1)[0].rsplit("/", 1)[1][:-3]
    return {"node_key": key, "title": mod, "description": "", "prompt_template": f"Python module `{mod}.py`.",
            "output_text": f"```python\n{code}```", "status": "done"}


# ---------------------------------------------------------------------------- the account's usage limit


def test_a_429_that_names_the_usage_limit_is_a_quota_and_a_plain_429_is_a_retry():
    quota = model_router.ModelResponse(model="m", success=False,
                                       error="HTTP 429: you (smokie) have reached your weekly usage limit")
    assert model_router._classify_failure(quota) == "quota"
    assert model_router.quota_exhausted(quota.error)
    plain = model_router.ModelResponse(model="m", success=False, error="HTTP 429: too many requests")
    assert model_router._classify_failure(plain) == "retry"
    assert not model_router.quota_exhausted(plain.error)
    assert not model_router.quota_exhausted("HTTP 500: quota")                   # a 500 mentioning quota is not the account's
    assert model_router.quota_exhausted(model_router.QUOTA_PREFIX + " (m): …")


async def test_a_quota_is_not_retried_and_does_not_fall_back_to_the_local_model():
    log = []

    async def fake_call(endpoint, payload, model, timeout):
        log.append(model)
        return model_router.ModelResponse(model=model, success=False,
                                          error="HTTP 429: you have reached your weekly usage limit")

    with patch.object(model_router, "_call_ollama", side_effect=fake_call), \
         patch.object(model_router, "_sleep_for_attempt", AsyncMock()):
        resp = await model_router._dispatch_with_retry("/api/chat", {}, "deepseek-v4-pro:cloud",
                                                       fallback="qwen3.5:latest", max_retries=3)
    assert log == ["deepseek-v4-pro:cloud"]                                      # one call: no retry, no fallback
    assert resp.success is False and resp.fallback_used is False
    assert (resp.error or "").startswith(model_router.QUOTA_PREFIX + " (deepseek-v4-pro:cloud): HTTP 429")
    assert "the run stops rather than falling back to qwen3.5:latest" in (resp.error or "")
    assert ea._quota_hit(resp.error)


async def test_a_node_that_failed_on_the_quota_blocks_the_job_with_the_reason_and_no_retry():
    seen: dict = {}

    async def fake_transition(db, job_id, *, to, expected_from=None, set_columns=None, reason="", **kw):
        seen.update({"to": to, "from": tuple(expected_from), "cols": dict(set_columns or {}), "reason": reason})
        return "running"

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            seen["committed"] = True

    res = {"node_key": "T5", "title": "Write tests",
           "error": model_router.QUOTA_PREFIX + " (m): HTTP 429: weekly usage limit"}
    with patch.object(ea, "async_session", lambda: _DB()), \
         patch("app.modules.job_state.transition", side_effect=fake_transition):
        events = [e async for e in ea._block_for_quota("job-1", "T5", res)]
    assert seen["to"] == "blocked" and seen["from"] == ("running", "executing") and seen["committed"]
    assert seen["reason"] == "model_quota_exhausted"
    assert seen["cols"]["error_summary"].startswith(model_router.QUOTA_PREFIX)
    assert events[0].startswith("event: node_failed") and '"retries_exhausted": true' in events[0] \
        and '"quota_exhausted": true' in events[0]
    assert events[1].startswith("event: warning") and '"reason": "model_quota_exhausted"' in events[1] \
        and "press Run again" in events[1]
    # both executor loops stop at the block, before the retry
    import inspect
    for fn in (ea._run_parallel_frontier, ea.execute_all_nodes):
        src = inspect.getsource(fn)
        assert src.index("_quota_hit(") < src.index("retry_failed_node(") and "_block_for_quota(" in src


# ---------------------------------------------------------------------------- a transaction the test before left open


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "docker" / "enginetest" / "server.py"


def _server():
    if not SERVER.exists():
        pytest.skip("docker/ is not in this image")
    import importlib.util
    spec = importlib.util.spec_from_file_location("enginetest_server_1889", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_isolation_plugin_waits_a_bounded_time_names_the_holder_and_ends_it():
    plugin = _server().ISOLATION_PLUGIN
    import ast
    ast.parse(plugin)
    assert "SET lock_timeout" in plugin and "asyncpg.LockNotAvailableError" in plugin
    assert "pg_stat_activity" in plugin and "pg_terminate_backend" in plugin
    assert "def pytest_runtest_logfinish(nodeid, location)" in plugin          # the test before, by name
    assert "transaction open on them" in plugin and "THIS test did not run" in plugin
    rx, hint = et.KNOWN_CAUSES[8]
    assert rx.search("E   Failed: §17.1889 the job's tables could not be emptied before this test: the test before it "
                     "(tests/test_x.py::test_a) left a transaction open on them -- a connection 42 (idle in transaction, "
                     "open 0:00:01), last statement `INSERT INTO approval …`")
    assert "must be closed inside that test" in hint


@pytest.mark.integration
async def test_a_leaked_transaction_is_named_terminated_and_the_run_goes_on(tmp_path):
    """Real pytest, real Postgres: test_a holds a transaction on the table and never ends it."""
    import asyncpg

    from app.config import settings
    srv = _server()
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    table = f"leak_probe_{uuid.uuid4().hex[:8]}"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'CREATE TABLE "{table}" (id serial PRIMARY KEY, v int NOT NULL)')
        (tmp_path / "_enginetest_isolation.py").write_text(
            srv.ISOLATION_PLUGIN.replace('_LOCK_WAIT = "3s"', '_LOCK_WAIT = "1s"'))
        (tmp_path / "test_leak.py").write_text(
            "import asyncio, os, asyncpg\n"
            "_KEEP = []\n"
            "async def _leak():\n"
            "    c = await asyncpg.connect(os.environ['ENGINETEST_DB_DSN'])\n"
            "    tr = c.transaction(); await tr.start()\n"
            f"    await c.execute('INSERT INTO \"{table}\" (v) VALUES (1)')\n"
            "    _KEEP.append((c, tr))\n"                                    # never committed, never closed
            "def test_a_leaks():\n    asyncio.run(_leak())\n"
            "def test_b_after_the_leak():\n    assert True\n"
            "def test_c_runs():\n    assert True\n")
        env = {**os.environ, "ENGINETEST_JOB_TABLES": table, "ENGINETEST_DB_DSN": dsn,
               "PYTEST_PLUGINS": "_enginetest_isolation", "PYTHONPATH": str(tmp_path)}
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "/dev/null",
                              "--rootdir", str(tmp_path), str(tmp_path / "test_leak.py")],
                             cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
        out = run.stdout + run.stderr
        assert "ERROR at setup of test_b_after_the_leak" in out, out
        assert "the test before it (test_leak.py::test_a_leaks) left a transaction open" in out, out
        assert "idle in transaction" in out and f'INSERT INTO "{table}"' in out, out
        assert "1 error" in out and "2 passed" in out and run.returncode != 0, out   # a, c ran; b was the report
        assert await conn.fetchval(f'SELECT count(*) FROM "{table}"') == 0  # the holder was ended, the table emptied
    finally:
        await conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        await conn.close()


# ---------------------------------------------------------------------------- an exit that swallows


def test_an_aexit_that_returns_a_mock_is_refused_and_one_that_returns_false_is_not():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock, patch

async def test_missing_secrets_raises():
    with patch("app.modules.paper_alpaca_paper_client.async_session") as mock_session:
        mock_session.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_session.return_value.__aexit__ = AsyncMock()
        cm = MagicMock(__aexit__=AsyncMock(return_value=True))
        other = MagicMock()
        other.__exit__ = MagicMock()
'''
    got = swallowing_exits({PATH: test})
    assert got == [
        "`tests/test_paper_twelve_modules.py:7` sets `mock_session.return_value.__aexit__` to `AsyncMock()`: an "
        "`__aexit__` that returns a truthy value (a mock's default return is a `MagicMock`) suppresses every exception "
        "raised inside the `with` -- a `pytest.raises` around it sees nothing (\"DID NOT RAISE\"), and the code after "
        "the `with` runs as if nothing failed -- `AsyncMock(return_value=False)`, or leave `__aexit__` as the "
        "`MagicMock` makes it (it returns False)",
        "`tests/test_paper_twelve_modules.py:8` sets `MagicMock(…, __aexit__=…)` to `AsyncMock(return_value=True)`: an "
        "`__aexit__` that returns a truthy value (a mock's default return is a `MagicMock`) suppresses every exception "
        "raised inside the `with` -- a `pytest.raises` around it sees nothing (\"DID NOT RAISE\"), and the code after "
        "the `with` runs as if nothing failed -- `AsyncMock(return_value=False)`, or leave `__aexit__` as the "
        "`MagicMock` makes it (it returns False)",
        "`tests/test_paper_twelve_modules.py:10` sets `other.__exit__` to `MagicMock()`: an `__exit__` that returns a "
        "truthy value (a mock's default return is a `MagicMock`) suppresses every exception raised inside the `with` -- "
        "a `pytest.raises` around it sees nothing (\"DID NOT RAISE\"), and the code after the `with` runs as if nothing "
        "failed -- `MagicMock(return_value=False)`, or leave `__exit__` as the `MagicMock` makes it (it returns False)"], got
    good = test.replace("__aexit__ = AsyncMock()", "__aexit__ = AsyncMock(return_value=False)") \
               .replace("AsyncMock(return_value=True)", "AsyncMock(return_value=None)") \
               .replace("other.__exit__ = MagicMock()", "pass")
    assert swallowing_exits({PATH: good}) == []
    assert swallowing_exits({"app/modules/x.py": test}) == []


# ---------------------------------------------------------------------------- a returned compare, a returned literal


def test_a_returned_compare_is_read_against_the_dict_given_and_the_modules_constant():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules import paper_fill_tracker
from app.modules.paper_fill_tracker import is_trade_update

def test_is_trade_update():
    envelope = {"stream": "other"}
    assert paper_fill_tracker.is_trade_update(envelope) is True
    assert is_trade_update({"stream": "trade_updates"})
    assert not is_trade_update({"stream": "subscription"})
    assert is_trade_update({"stream": "subscription"}) == True
    assert is_trade_update({"T": "trade_updates"}) is True
'''
    got = returned_compares({PATH: test}, MODULES)
    assert got == [
        "`tests/test_paper_twelve_modules.py:7` asserts `is_trade_update(…)` is True, but it returns "
        "`envelope.get('stream') == TRADE_UPDATES_STREAM` (`TRADE_UPDATES_STREAM`, 'trade_updates'), which is False for "
        "the dict given (`stream` = 'other')",
        "`tests/test_paper_twelve_modules.py:10` asserts `is_trade_update(…)` is True, but it returns "
        "`envelope.get('stream') == TRADE_UPDATES_STREAM` (`TRADE_UPDATES_STREAM`, 'trade_updates'), which is False for "
        "the dict given (`stream` = 'subscription')"], got            # a missing key (line 11) is the keys gate's


def test_a_literal_the_function_never_returns_under_a_key_is_refused():
    test = '''# tests/test_paper_twelve_modules.py
from app.modules.paper_operator_view import get_operator_view

async def test_view_empty(db):
    view = await get_operator_view(db=db)
    assert view["metrics"] == {} or view["metrics"] is None
    assert view["metrics"] in ({}, None)
    assert view["orders"] == 0
    assert view["orders"] == []
    assert view["metrics"] is not None
'''
    got = returned_compares({PATH: test}, MODULES)
    never = ("`get_operator_view` (`paper_operator_view`) never returns {} under `metrics`: it is "
             "`{'cumulative_return': None, 'max_drawdown': None}` or `dict(metrics_row._mapping)` -- assert the keys it carries")
    assert got[:3] == [
        f"`tests/test_paper_twelve_modules.py:6` asserts `view['metrics'] == {{}}`, but {never}",
        "`tests/test_paper_twelve_modules.py:6` asserts `view['metrics'] is None`, but " + never.replace("returns {}", "returns None"),
        "`tests/test_paper_twelve_modules.py:7` asserts `view['metrics'] in ({}, None)`, but "
        + never.replace("returns {}", "returns {} or None")], got
    assert len(got) == 4 and got[3].startswith("`tests/test_paper_twelve_modules.py:8` asserts `view['orders'] == 0`, but "
                                                "`get_operator_view` (`paper_operator_view`) returns `[dict(r._mapping) for r in")
    assert got[3].endswith("under `orders` -- a list compared with a number is never equal: compare `len(view['orders'])`")
    # `== []` can hold (the list may be empty); `is not None` holds


# ---------------------------------------------------------------------------- a text whose every row the parser drops


def test_a_text_whose_every_row_is_too_narrow_is_refused_unless_the_test_expects_nothing():
    test = '''# tests/test_paper_twelve_modules.py
from unittest.mock import AsyncMock, MagicMock
from app.modules.paper_finra_data_ingestion import ingest_finra_file

async def test_short_interest():
    http_get = AsyncMock(return_value=MagicMock(text="Date|Symbol|ShortInterest\\n20261025|AAPL|100\\n"))
    result = await ingest_finra_file(source="short_interest", http_get=http_get)
    assert result["ingested_items"] == 1

async def test_header_only():
    http_get = AsyncMock(return_value=MagicMock(text="header\\nrow1\\n"))
    result = await ingest_finra_file(source="short_interest", http_get=http_get)
    assert "ingested_items" in result

async def test_commas():
    http_get = AsyncMock(return_value=MagicMock(text="SPY,1000,2.5,1.2,20260101\\n"))
    result = await ingest_finra_file(source="short_interest", http_get=http_get)
    assert result["ingested_items"] >= 1

async def test_empty_on_purpose():
    http_get = AsyncMock(return_value=MagicMock(text="junk\\n"))
    result = await ingest_finra_file(source="short_interest", http_get=http_get)
    assert result["ingested_items"] == 0

async def test_wide_enough():
    http_get = AsyncMock(return_value=MagicMock(text="20261025|AAPL|100|10|2.5\\n20261025|SPY|5|1|0.2\\n"))
    result = await ingest_finra_file(source="short_interest", http_get=http_get)
    assert result["ingested_items"] == 2
'''
    got = narrow_rows({PATH: test}, MODULES)
    tail = ("where `_parse_short_interest` (`paper_finra_data_ingestion`) skips every row with fewer than 5 "
            "(`settlement_date|symbol|short_interest|avg_daily_volume|days_to_cover`): nothing is parsed, nothing "
            "counted -- give it rows of 5 `|`-separated fields")
    assert got == [
        "`tests/test_paper_twelve_modules.py:6` hands `ingest_finra_file(…)` text whose rows have 3 field(s) "
        "(`Date|Symbol|ShortInterest`, `20261025|AAPL|100`) " + tail,
        "`tests/test_paper_twelve_modules.py:11` hands `ingest_finra_file(…)` text whose rows have 1 field(s) "
        "(`header`, `row1`) " + tail,
        "`tests/test_paper_twelve_modules.py:16` hands `ingest_finra_file(…)` text whose rows have 1 field(s) "
        "(`SPY,1000,2.5,1.2,20260101`) " + tail], got


# ---------------------------------------------------------------------------- a method of a job class


def test_a_method_called_on_an_instance_is_checked_against_its_signature():
    test = '''# tests/test_paper_twelve_modules.py
import pytest
from decimal import Decimal
from app.modules import paper_alpaca_paper_client
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient, AlpacaPaperClientError

async def test_submit_order_rejects_bad_side():
    client = AlpacaPaperClient()
    with pytest.raises(AlpacaPaperClientError):
        await client.submit_order(symbol="SPY", qty=Decimal("1"), side="invalid")

async def test_submit_order_bad_side_only():
    client = paper_alpaca_paper_client.AlpacaPaperClient()
    with pytest.raises(AlpacaPaperClientError):
        await client.submit_order(symbol="SPY", qty=Decimal("1"), side="hold", order_type="market")

async def test_submit_order_ok():
    client = AlpacaPaperClient(base_url="https://x")
    result = await client.submit_order(symbol="SPY", qty="1", side="buy", order_type="market")
'''
    defs = es.sibling_definitions([_row("T8", ALPACA)])
    got = [f.split(" -- the signature")[0] for f in es.sibling_refs(f"```python\n{test}```", defs)]
    assert got == [
        "`tests/test_paper_twelve_modules.py:10` calls `AlpacaPaperClient.submit_order(…)` is missing required "
        "argument(s) `order_type`"], got                                  # `side="invalid"` under raises: exempt


# ---------------------------------------------------------------------------- JSON the engine cannot carry


def test_a_value_json_cannot_carry_is_blamed_on_the_jobs_module_above_the_engine_frame():
    rx, hint = et.KNOWN_CAUSES[9]
    assert rx.search("E   TypeError: Object of type Decimal is not JSON serializable")
    assert "the module that builds that dict converts it first" in hint
    assert et.KNOWN_CAUSES[7][0].search("E   AssertionError: assert Decimal('1.000000') == '1'")      # NUMERIC via Decimal
    report = """
_________________ test_approval_gate_refuses_unapproved_orders _________________
tests/test_paper_twelve_modules.py:334: in test_approval_gate_refuses_unapproved_orders
    result = await request_approval(proposal)
app/modules/paper_operator_approval_gate.py:116: in request_approval
    job_id = await open_decision_job(
app/modules/operator_ask.py:131: in open_decision_job
    "c": json.dumps({"decision": {"options": d["options"]}, "payload": d["payload"]})})
  … 4 library frames …
E   TypeError: Object of type Decimal is not JSON serializable
E   when serializing dict item 'quantity'
=========================== short test summary info ============================
"""
    d = et.callee_defects(report)
    assert len(d) == 1 and d[0]["module"] == "operator_ask"
    assert d[0]["modules"] == ["paper_operator_approval_gate", "operator_ask"]
    import inspect
    from app.modules import engine_plan
    src = inspect.getsource(engine_plan)
    assert 'reversed(d.get("modules") or [d["module"]])' in src            # the deepest frame that is the job's own
