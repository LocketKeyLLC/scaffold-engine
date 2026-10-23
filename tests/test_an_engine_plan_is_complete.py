"""§17.1766 — a plan for code that extends the engine is completed before it runs: each module tested in the step
that writes it, no engine module's name taken, the asking step made asynchronous, one step that wires the parts in.

Live (job 2772acae, 28 steps): no step joined the nineteen modules (no call sites at all), the one test step came last
and failed on bugs in all of them at once, T12 was planned as `kill_switch.py` (the engine's VPN kill switch), and T14
was specified as `request_approval(proposal) -> bool`. The plan below is cut down from that one; titles and module
names are the live ones.
"""
from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.modules import dag_generator, engine_plan

BRIEF = {"title": "Paper-trading research lab in scaffold engine",
         "description": "Add a paper-trading research lab to the scaffold engine's own codebase.",
         "goals": ["Add a paper-trading research lab module to this repository's code."]}

PLAN = [
    {"id": "T2", "name": "Define Postgres audit schema", "tool": "CodeGen", "depends_on": [],
     "notes": "Create tables: signals, proposals, risk_checks."},
    {"id": "T9", "name": "Implement Alpaca paper client", "tool": "CodeGen", "depends_on": [],
     "notes": "Python module `alpaca_client.py` with functions `get_account() -> dict`."},
    {"id": "T12", "name": "Implement kill switch", "tool": "CodeGen", "depends_on": ["T9"],
     "notes": "Python module `kill_switch.py` with `activate_kill_switch() -> dict`."},
    {"id": "T14", "name": "Implement approve-before-run step", "tool": "CodeGen", "depends_on": [],
     "notes": "Python module `approval_step.py` with `request_approval(proposal) -> bool`."},
    {"id": "T15", "name": "Implement order executor", "tool": "CodeGen", "depends_on": ["T9", "T14", "T12"],
     "notes": "Python module `order_executor.py` with `execute_approved_order(proposal) -> dict`; uses "
              "app.modules.kill_switch to check the halt."},
    {"id": "T22", "name": "Write integration tests", "tool": "CodeGen", "depends_on": ["T9", "T12", "T15"],
     "notes": "Python module `test_integration.py` with `def test_*` functions."},
    {"id": "T28", "name": "Document the lab", "tool": "LLM", "depends_on": ["T22"], "notes": "README."},
]


def _plan():
    return copy.deepcopy(PLAN)


def test_the_live_plan_is_completed():
    tasks = _plan()
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert "`paper_kill_switch.py`" in by["T12"]["notes"]
    assert "app.modules.paper_kill_switch" in by["T15"]["notes"]                # renamed everywhere it is named
    for k, mod in (("T9", "alpaca_client"), ("T12", "paper_kill_switch"), ("T14", "approval_step"),
                   ("T15", "order_executor")):
        assert f"tests/test_{mod}.py" in by[k]["notes"], k
    assert "tests/test_test_integration" not in json.dumps(tasks)               # a test step is not a module
    assert "Python module" not in by["T2"]["notes"]                              # a schema step is a revision
    assert "operator_ask" in by["T14"]["notes"] and "operator_ask" not in by["T15"]["notes"]
    wire = by["WIRE"]
    assert wire["tool"] == "CodeGen" and set(wire["depends_on"]) == {"T9", "T12", "T14", "T15"}
    assert "`paper_wiring.py`" in wire["notes"] and "def install(app)" in wire["notes"]
    assert any(c.startswith("wiring_step_added") for c in changes)


def test_completing_twice_changes_nothing_more():
    tasks = _plan()
    engine_plan.complete_engine_plan(tasks, BRIEF)
    before = copy.deepcopy(tasks)
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []
    assert tasks == before


def test_a_plan_that_already_wires_and_tests_is_left_alone():
    tasks = [
        {"id": "A", "name": "Implement client", "tool": "CodeGen", "depends_on": [],
         "notes": "Python module `paper_client.py`, with tests in `tests/test_paper_client.py`."},
        {"id": "B", "name": "Implement runner", "tool": "CodeGen", "depends_on": ["A"],
         "notes": "Python module `paper_runner.py` and `tests/test_paper_runner.py`; register it with "
                  "get_scheduler() in app/main.py."},
    ]
    before = copy.deepcopy(tasks)
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []
    assert tasks == before


def test_the_prefix_comes_from_the_brief():
    assert engine_plan.prefix_for(BRIEF) == "paper"
    assert engine_plan.prefix_for({"title": "Add a lab"}) == "job"


# ── the production path ──────────────────────────────────────────────────────

async def _generate(brief: dict) -> tuple[list[dict], list[str]]:
    async def _stub(brief_data, route_kwargs):
        return {"dag_data": {"strategy": "sequential", "tasks": [
                    {**t, "type": "action", "inputs": [], "outputs": [f"o_{t['id']}"], "domain": None,
                     "assigned_model": None, "is_deliverable": t["id"] == "T28"} for t in _plan()]},
                "raw_text": "", "model": "m", "duration_ms": 0, "warnings": [], "error": None, "attempts": 1,
                "validator_calls": 0}

    row = MagicMock()
    row.first.return_value = ("planning", brief, None, None, None, 0, False)
    inserted: list[dict] = []

    async def _execute(stmt, params=None, *a, **k):
        if params and "node_key" in params and "prompt_template" in params:
            inserted.append(dict(params))
        return row
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    warnings: list[str] = []
    with patch.object(dag_generator, "_generate_dag_with_validator", _stub), \
         patch.object(dag_generator, "_fail_job", new_callable=AsyncMock):
        try:
            out = await dag_generator.generate_dag(str(uuid4()), db)
            warnings = list((out or {}).get("validation", {}).get("warnings") or []) if isinstance(out, dict) else []
        except Exception:                                    # the persistence tail is not under test
            pass
    return inserted, warnings


async def test_generate_dag_completes_a_plan_for_the_engine():
    inserted, _ = await _generate(BRIEF)
    keys = {p["node_key"]: p for p in inserted}
    assert "WIRE" in keys, sorted(keys)
    assert "tests/test_paper_kill_switch.py" in keys["T12"]["prompt_template"]


async def test_generate_dag_leaves_another_jobs_plan_alone():
    inserted, _ = await _generate({"title": "News CLI", "description": "Write a news CLI.", "goals": ["A CLI."]})
    keys = {p["node_key"]: p for p in inserted}
    assert keys and "WIRE" not in keys
    assert "tests/test_" not in keys["T12"]["prompt_template"]


# ── §17.1767 a code step that names no file (live job 918b6274, 2026-10-23) ──

PLAN_2 = [
    {"id": "T3", "name": "Implement audit trail writes", "tool": "CodeGen", "depends_on": [],
     "notes": "Exports append_signal(), append_proposal(). Uses existing Postgres connection."},
    {"id": "T6", "name": "Implement kill switch", "tool": "CodeGen", "depends_on": [],
     "notes": "Exports activate_kill_switch(). Consumed by approval flow and operator dashboard."},
    {"id": "T7", "name": "Implement supervised approval flow", "tool": "CodeGen", "depends_on": ["T3", "T6"],
     "notes": "Exports request_approval(proposal) and execute_approved(proposal)."},
    {"id": "T24", "name": "Store broker order ids", "tool": "CodeGen", "depends_on": ["T7"],
     "notes": "Exports store_broker_order_id(order_id, proposal_id). Called by approval flow after execution."},
    {"id": "T27", "name": "Validate risk controls", "tool": "LLM", "depends_on": ["T6", "T7"], "notes": "Check."},
]


def test_a_step_that_names_no_file_gets_one_from_its_title():
    tasks = copy.deepcopy(PLAN_2)
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert by["T3"]["notes"].startswith("Python module `paper_audit_trail_writes.py`.")
    assert by["T6"]["notes"].startswith("Python module `paper_kill_switch.py`.")        # never the engine's own name
    assert "tests/test_paper_audit_trail_writes.py" in by["T3"]["notes"]
    assert "Python module" not in by["T27"]["notes"]                                   # not a code step
    assert set(by["WIRE"]["depends_on"]) == {"T3", "T6", "T7", "T24"}
    assert sum(c.startswith("module_named") for c in changes) == 4
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


def test_only_the_step_that_asks_is_made_asynchronous():
    tasks = copy.deepcopy(PLAN_2)
    engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert "operator_ask" in by["T7"]["notes"]                                         # exports request_approval
    assert "operator_ask" not in by["T6"]["notes"] and "operator_ask" not in by["T24"]["notes"]   # only mention it


def test_names_are_unique_and_short():
    tasks = [{"id": f"A{i}", "name": "Implement the point-in-time price data loader", "tool": "CodeGen",
              "depends_on": []} for i in range(3)]
    engine_plan.complete_engine_plan(tasks, BRIEF)
    names = [engine_plan._module_of(t) for t in tasks[:3]]
    assert names == ["paper_point_time_price_data", "paper_point_time_price_data_2", "paper_point_time_price_data_3"]


# ── §17.1768 tables are a migration; a test step writes a test module (live job 918b6274's re-plan) ──

def test_a_shell_step_running_ddl_becomes_an_alembic_revision():
    from app.modules.engine_source import _latest_migration
    tasks = [{"id": "T3", "name": "Create audit trail tables", "tool": "Shell", "depends_on": ["T2"],
              "notes": "Starts from T2's schema design. Runs CREATE TABLE statements in the existing Postgres database."},
             {"id": "T9", "name": "Check the VPN", "tool": "Shell", "depends_on": [], "notes": "ip a"}]
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    nxt = f"{int(_latest_migration()[:4]) + 1:04d}"
    assert tasks[0]["tool"] == "CodeGen"
    assert f"alembic/versions/{nxt}_paper_audit_trail_tables.py" in tasks[0]["notes"]
    assert "Python module" not in tasks[0]["notes"]                              # a revision, not an app module
    assert "alembic" not in tasks[1]["notes"]                                    # a shell step that runs no DDL
    assert tasks[1]["notes"].startswith("Python test module")                    # is a check (§17.1770)
    assert any(c.startswith("ddl_made_a_migration: T3") for c in changes)


def test_a_step_that_writes_tests_writes_a_test_module():
    tasks = [{"id": "A", "name": "Implement order executor", "tool": "CodeGen", "depends_on": []},
             {"id": "B", "name": "Implement risk limits", "tool": "CodeGen", "depends_on": []},
             {"id": "T31", "name": "Write integration tests", "tool": "CodeGen", "depends_on": ["A", "B"],
              "notes": "Python test files that import from all upstream modules."}]
    engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert by["T31"]["notes"].startswith("Python test module `tests/test_paper_integration.py`")
    assert "test_test_" not in by["T31"]["notes"] and engine_plan._module_of(by["T31"]) is None
    assert "T31" not in by["WIRE"]["depends_on"]                                 # tests are not wired in


# ── §17.1769 a migration step, a shell test run, and the operator's keys (live job 918b6274's third plan) ──

PLAN_3 = [
    {"id": "T3", "name": "Write audit trail migration", "tool": "CodeGen", "type": "action", "depends_on": ["T2"],
     "notes": "Executable SQL migration implementing T2's schema. Creates tables only; no data writes."},
    {"id": "T4", "name": "Integrate Alpaca paper credentials", "tool": "Shell", "type": "action", "depends_on": [],
     "notes": "Store paper-trading key in existing secrets store. Verify retrieval works."},
    {"id": "T5", "name": "Write Alpaca client module", "tool": "CodeGen", "type": "action", "depends_on": ["T4"],
     "notes": "Exports place_order()."},
    {"id": "T6", "name": "Write risk control module", "tool": "CodeGen", "type": "action", "depends_on": []},
    {"id": "T28", "name": "Write integration tests", "tool": "CodeGen", "type": "action", "depends_on": ["T5", "T6"]},
    {"id": "T29", "name": "Run integration tests", "tool": "Shell", "type": "validation", "depends_on": ["T28"],
     "notes": "Execute test suite. Verify secrets redaction, risk controls."},
    {"id": "T30", "name": "Final integration validation", "tool": "LLM", "type": "validation", "depends_on": ["T29"]},
]


def test_the_third_live_plan_is_completed():
    from app.modules.engine_source import _latest_migration
    tasks = copy.deepcopy(PLAN_3)
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    nxt = f"{int(_latest_migration()[:4]) + 1:04d}"
    assert f"alembic/versions/{nxt}_paper_audit_trail.py" in by["T3"]["notes"] and engine_plan._module_of(by["T3"]) is None
    assert "T29" not in by and by["T30"]["depends_on"] == ["T28"]                 # the sandbox runs T28's tests
    assert by["T4"]["type"] == "decision" and by["T4"]["tool"] == "LLM"
    assert by["T4"]["name"] == "Are the Alpaca paper keys stored in the engine's secrets?"
    assert by["T4"]["depends_on"] == ["WIRE"] and by["T5"]["depends_on"] == []    # asked at the end, blocks no build
    assert {c.split(":")[0] for c in changes} >= {"migration_named", "test_run_dropped", "secret_step_made_the_operators"}
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


# ── §17.1770 no Shell step in a plan for engine code (live job 918b6274's fourth plan) ──

PLAN_4 = [
    {"id": "T5", "name": "Implement Alpaca REST client", "tool": "CodeGen", "type": "action", "depends_on": []},
    {"id": "T16", "name": "Implement proposal generator", "tool": "CodeGen", "type": "action", "depends_on": ["T5"]},
    {"id": "T17", "name": "Run daily proposal batch", "tool": "Shell", "type": "action", "depends_on": ["T16"],
     "notes": "Run the batch before the open."},
    {"id": "T21", "name": "Execute approved orders", "tool": "Shell", "type": "action", "depends_on": ["T5"]},
    {"id": "T28", "name": "Verify no live trading keys stored in the engine", "tool": "Shell", "type": "validation",
     "depends_on": ["T5"], "notes": "Check the secrets store holds only paper keys."},
    {"id": "T30", "name": "Verify point-in-time integrity", "tool": "Shell", "type": "validation", "depends_on": ["T16"]},
]


def test_no_shell_step_is_left_in_a_plan_for_engine_code():
    tasks = copy.deepcopy(PLAN_4)
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert [t["id"] for t in tasks if t["tool"] == "Shell"] == []
    assert by["T17"]["notes"].startswith("Python module `paper_daily_proposal_batch.py`.")
    assert "tests/test_paper_daily_proposal_batch.py" in by["T17"]["notes"]
    assert {"T17", "T21"} <= set(by["WIRE"]["depends_on"])                        # the work they did is wired in
    assert by["T30"]["notes"].startswith("Python test module `tests/test_paper_point_time_integrity.py`")
    assert by["T30"]["type"] == "action" and "T30" not in by["WIRE"]["depends_on"]
    # a check about keys is a check, not the operator's keys to store (it was made a question by §17.1769)
    assert by["T28"]["type"] != "decision" and "tests/test_paper_no_live_trading_keys" in by["T28"]["notes"]
    assert not any(c.startswith("secret_step_made_the_operators") for c in changes)
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []
