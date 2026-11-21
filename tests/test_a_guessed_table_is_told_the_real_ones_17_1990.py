"""§17.1990 — a guessed table is told the real ones; a module the output rewrites has what the rewrite defines.

Live (job 69fe61c4 "Paper-Trading Lab Operator Dashboard", T8 "Add dashboard route", 2026-11-21 22:00–22:37 UTC, six
attempts): every draft invented a table for "the full audit trail" and the SPY bars -- `paper_audit_trail` (13
refusals), `paper_spy_bars` (4), `paper_spy_daily` (2) -- and each refusal said only "there is no table `X` … (a new
table is an Alembic revision …)", so each redraft guessed again. The lab's audit trail is the seven tables
`paper_audit_trail_writer` writes; SPY bars are stored nowhere (`paper_spy_benchmark_tracker` fetches them;
`pnl.spy_benchmark_return` keeps the return). The drafter's "engine's own code" block showed no engine table (the job
adds no migration), and told it `paper_operator_view.py` -- merged -- was "NOT importable here". And its tests'
`from app.modules.paper_wire_engine_entry_module import _collect_dashboard_data, load_audit_trail, …` -- names the
output's own rewrite of that module defines -- were refused "has no … (it has install)" against the file being
replaced."""
from __future__ import annotations

import importlib.util

import pytest

from app.modules import engine_plan as ep
from app.modules import engine_schema as sch
from app.modules import engine_source as es


@pytest.fixture(scope="module")
def _whole_index():
    spec = importlib.util.spec_from_file_location("_es_whole_17_1990", es.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    return fresh.module_index()


@pytest.fixture(autouse=True)
def _the_engine_with_its_lab(monkeypatch, _whole_index):
    """The live case IS the delivered lab: these tests read the engine whole (the conftest hides `paper_*`, §17.1935)."""
    index = _whole_index
    monkeypatch.setattr(es, "module_index", lambda: index)
    monkeypatch.setattr(ep, "module_index", lambda: index)
    yield

#: the lab's tables as its migration makes them (columns cut to what the assertions read)
LAB = {
    "signal": {"id", "ticker", "direction", "rationale", "ingested_item_id"},
    "proposal": {"id", "signal_id", "ticker", "side", "quantity", "rationale"},
    "risk_check": {"id", "proposal_id", "passed", "reason"},
    "approval": {"id", "proposal_id", "decision", "operator_id"},
    "broker_order_id": {"id", "proposal_id", "approval_id", "broker_order_id"},
    "fill": {"id", "broker_order_id", "price", "quantity", "side", "commission"},
    "pnl": {"id", "as_of_ts", "equity", "account_value", "daily_return", "spy_benchmark_return", "turnover"},
    "ingested_item": {"id", "source", "payload"},
    "jobs": {"id", "status"},
    "error_logs": {"id", "message"},
    "execution_logs": {"id", "message", "log_level"},
}

#: T8's attempt (llm_traces 13525), cut to the SQL the refusal named
T8_OUTPUT = '''```python
# app/modules/paper_wire_engine_entry_module.py
from sqlalchemy import text
from app.database import async_session
from app.modules.paper_operator_view import get_operator_view

AUDIT_TABLE = "paper_audit_trail"


async def _audit_loader(db):
    rows = (await db.execute(text("SELECT id, kind, payload, created_at FROM paper_audit_trail ORDER BY id DESC"))).all()
    return [dict(r._mapping) for r in rows]


async def _benchmark_loader(db):
    rows = (await db.execute(text("SELECT day, close FROM paper_spy_bars WHERE symbol = 'SPY' ORDER BY day"))).all()
    return [dict(r._mapping) for r in rows]


def install(app):
    return None
```
'''


def _users():
    return es.engine_table_users()


# ── (1) the no-table refusal names the real tables ───────────────────────────────────────────────────────────────

def test_the_lab_modules_are_read_for_the_tables_they_write_and_read():
    u = _users()
    assert u["paper_audit_trail_writer"]["writes"] >= {"signal", "proposal", "risk_check", "approval",
                                                       "broker_order_id", "fill", "pnl"}
    assert {"proposal", "fill", "pnl"} <= u["paper_operator_view"]["reads"]
    assert not (u["paper_spy_benchmark_tracker"]["writes"] | u["paper_spy_benchmark_tracker"]["reads"])
    assert "get_spy_benchmark" in u["paper_spy_benchmark_tracker"]["funcs"]


def test_t8s_audit_trail_guess_is_told_the_writer_and_its_tables():
    found = sch.unknown_schema_refs(T8_OUTPUT, LAB, users=_users(), using=es.output_uses(T8_OUTPUT))
    audit = next(f for f in found if "no table `paper_audit_trail`" in f)
    assert ("`paper_audit_trail_writer` writes `approval`, `broker_order_id`, `fill`, `pnl`, `proposal`, "
            "`risk_check`, `signal`") in audit
    # and what the modules it imports read
    assert "`paper_operator_view` reads" in audit and "`pnl`" in audit


def test_t8s_spy_bars_guess_is_told_they_are_not_stored_and_what_computes_them():
    found = sch.unknown_schema_refs(T8_OUTPUT, LAB, users=_users())
    spy = next(f for f in found if "no table `paper_spy_bars`" in f)
    assert "the engine stores no such data -- `paper_spy_benchmark_tracker` computes it when called" in spy
    assert "get_spy_benchmark" in spy
    assert "`pnl.spy_benchmark_return`" in spy


def test_a_guess_like_a_real_table_is_told_that_table():
    msg = sch.where_a_table_is("paper_fills", LAB, _users())
    assert "tables named like it: `fill`" in msg


def test_a_generic_word_names_nothing():
    # `log` is in error_logs and execution_logs, `paper` in every lab module: neither is a hint
    msg = sch.where_a_table_is("paper_widget_log", {**LAB, "llm_call_logs": {"id"}, "dedup_log": {"id"}}, _users())
    assert "error_logs" not in msg and "execution_logs" not in msg
    assert sch.where_a_table_is("zzqx_frobs", LAB, _users()) == ""


def test_a_real_table_is_not_refused():
    out = "```python\nQ = \"SELECT id, equity FROM pnl ORDER BY as_of_ts\"\n```\n"
    assert sch.unknown_schema_refs(out, LAB, users=_users()) == []


# ── (2) a module the output rewrites has what the rewrite defines ────────────────────────────────────────────────

REWRITE = '''```python
# app/modules/paper_wire_engine_entry_module.py
LAB_SLUG = "paper"
ALLOWED_SPANS = ("1w", "1m", "3m", "all")


async def load_audit_trail(db):
    return []


async def _collect_dashboard_data(db):
    return {}, [], []


def install(app):
    return None
```

```python
# tests/test_paper_wire_engine_entry_module.py
from app.modules.paper_wire_engine_entry_module import ALLOWED_SPANS, _collect_dashboard_data, load_audit_trail
from app.modules.paper_wire_engine_entry_module import spy_from_view
```
'''


def test_names_the_rewrite_defines_are_not_refused_and_one_it_lacks_is():
    found = [f for f in es.unresolved_refs(REWRITE) if "paper_wire_engine_entry_module import" in f]
    assert not any(f"import {n}`" in f for f in found for n in ("ALLOWED_SPANS", "_collect_dashboard_data",
                                                               "load_audit_trail"))
    assert len(found) == 1
    assert "this output's `app/modules/paper_wire_engine_entry_module.py` defines no `spy_from_view`" in found[0]
    assert "load_audit_trail" in found[0] and "ALLOWED_SPANS" in found[0]


def test_a_missing_name_lists_the_modules_functions_and_constants_and_where_the_thing_is():
    out = "```python\nfrom app.modules.paper_wire_engine_entry_module import load_audit_trail\n```\n"
    msg = next(f for f in es.unresolved_refs(out) if "load_audit_trail" in f)
    assert "has no `load_audit_trail` (it has install, " in msg and "ROUTER_PREFIX" in msg
    assert "`app.modules.paper_audit_trail_writer` has write_approval" in msg
    out = "```python\nfrom app.modules.paper_wire_engine_entry_module import _audit_loader\n```\n"
    msg = next(f for f in es.unresolved_refs(out) if "_audit_loader" in f)
    assert "_make_router" in msg                          # a private name asked for: the private functions too


# ── (3) the drafter is shown the engine tables its subject uses ──────────────────────────────────────────────────

T8_NODE = {"node_key": "T8", "title": "Add dashboard route",
           "prompt_template": "Python module `paper_wire_engine_entry_module.py` -- the engine's own, which this job "
                              "extends: edit it in place. Thin route: imports build_context (T6), reuses the existing "
                              "API-key auth dependency, accepts span query param."}
T8_BRIEF = ('{"goals": ["Display account value vs SPY over time", "Display the full audit trail.", "Read every number '
            'from existing tables and functions (paper_operator_view.get_operator_view, the audit trail, the SPY '
            'benchmark)"]}')


def test_the_block_shows_the_tables_of_the_audit_trail_and_says_spy_is_not_stored():
    b = es.block_for(T8_NODE, {"paper_wire_engine_entry_module", "paper_operator_view", "paper_dashboard"},
                     written=set(), live=LAB, brief=T8_BRIEF)
    sec = b.split("### The engine's tables this step's subject uses", 1)[1].split("###", 1)[0]
    assert "`paper_audit_trail_writer` writes approval, broker_order_id, fill, pnl, proposal, risk_check, signal" in sec
    assert "`paper_operator_view` reads" in sec
    assert "`paper_spy_benchmark_tracker` keeps no table" in sec and "get_spy_benchmark" in sec
    assert "pnl: account_value, as_of_ts, daily_return" in sec
    assert "arr_api_key" not in sec                       # "API-key auth" is not the arr key module
    # a merged engine module is importable; a module no step has written yet is not
    assert "`paper_operator_view.py`" not in b.split("NOT importable here", 1)[1].split("\n", 1)[0]
    assert "`paper_dashboard.py`" in b.split("NOT importable here", 1)[1].split("\n", 1)[0]


def test_no_subject_named_no_section():
    b = es.block_for({"title": "Add dashboard route", "prompt_template": "Thin route."}, set(), live=LAB, brief="")
    assert "The engine's tables this step's subject uses" not in b
