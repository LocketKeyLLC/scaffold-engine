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

import pytest

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
    assert wire["tool"] == "CodeGen" and set(wire["depends_on"]) == {"T9", "T12", "T14", "T15", "T2"}   # + schema (§17.1774)
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
    assert by["T4"]["depends_on"] == ["WIRE"] and by["T5"]["depends_on"] == ["T3", "API_ALPACA"]  # asked last; schema + API research
    assert {c.split(":")[0] for c in changes} >= {"ddl_made_a_migration", "test_run_dropped", "secret_step_made_the_operators"}
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



# ── §17.1771 a step that writes the schema is the revision; a step that only applies it is dropped ──

def test_the_fifth_live_plan_writes_one_revision_and_applies_nothing_by_hand():
    from app.modules.engine_source import _latest_migration
    tasks = [
        {"id": "T2", "name": "Design audit trail schema", "tool": "CodeGen", "type": "action", "depends_on": [],
         "notes": "Emit SQL DDL for tables: signals, proposals, risk_checks. Append-only via triggers."},
        {"id": "T3", "name": "Apply audit trail schema", "tool": "CodeGen", "type": "action", "depends_on": ["T2"],
         "notes": "Run the DDL against the existing Postgres. Verify tables exist and UPDATE/DELETE are revoked."},
        {"id": "T4", "name": "Implement audit trail writer", "tool": "CodeGen", "type": "action", "depends_on": ["T3"],
         "notes": "Exports append_signal(). Uses existing Postgres connection."},
        {"id": "T33", "name": "Validate audit trail append-only", "tool": "LLM", "type": "validation",
         "depends_on": ["T3", "T4"]},
    ]
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    head = _latest_migration()
    rev = f"{int(head[:4]) + 1:04d}_paper_audit_trail"
    assert "T3" not in by and by["T4"]["depends_on"] == ["T2"] and by["T33"]["depends_on"] == ["T4", "T2"]
    assert by["T2"]["notes"].startswith(f"An Alembic revision `alembic/versions/{rev}.py`")
    assert f'down_revision = "{head}"' in by["T2"]["notes"] and engine_plan._module_of(by["T2"]) is None
    assert engine_plan._module_of(by["T4"]) == "paper_audit_trail_writer"          # the writer stays a module
    assert any(c.startswith("ddl_apply_dropped: T3") for c in changes)
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


# ── §17.1772 an LLM step doing the lab's work is code; "for approval" asks (live job 918b6274's sixth plan) ──

def test_an_llm_step_doing_the_labs_work_is_code_and_presenting_for_approval_asks():
    tasks = [
        {"id": "T13", "name": "Store signals to audit trail", "tool": "CodeGen", "type": "action", "depends_on": []},
        {"id": "T14", "name": "Generate trade proposals", "tool": "LLM", "type": "action", "depends_on": ["T13"],
         "notes": "From the stored signals, propose trades with a rationale."},
        {"id": "T17", "name": "Present proposals for approval", "tool": "CodeGen", "type": "action", "depends_on": ["T14"]},
        {"id": "T2", "name": "Design audit trail schema", "tool": "LLM", "type": "action", "depends_on": []},
        {"id": "T32", "name": "Document the lab", "tool": "LLM", "type": "action", "depends_on": ["T17"]},
        {"id": "T28", "name": "Validate no live key", "tool": "LLM", "type": "validation", "depends_on": ["T14"]},
    ]
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert by["T14"]["tool"] == "CodeGen" and engine_plan._module_of(by["T14"]) == "paper_trade_proposals"
    assert all(by[k]["tool"] == "LLM" for k in ("T2", "T32", "T28"))          # designing, documenting, reviewing
    assert "operator_ask" in by["T17"]["notes"]
    assert any(c.startswith("llm_work_made_code: T14") for c in changes)
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


# ── §17.1773 nothing promises to block; a step that requests and acts is split (live job 918b6274's seventh plan) ──

def test_no_step_blocks_on_an_answer_and_acting_on_it_is_the_handlers():
    tasks = [
        {"id": "T8", "name": "Implement supervised approval flow", "tool": "CodeGen", "type": "action", "depends_on": [],
         "notes": "Python module `approval_flow.py` exporting `ApprovalFlow` class. Method `request_approval(proposal)` "
                  "displays symbol and side. Blocks until operator approves or rejects. Records decision to audit trail."},
        {"id": "T26", "name": "Implement daily trading orchestrator", "tool": "CodeGen", "type": "action",
         "depends_on": ["T8"], "notes": "Python module `daily_orchestrator.py` exporting `run_daily_batch()`. Runs "
                                        "before market open: aggregate, risk check, request approval, execute approved orders."},
    ]
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert "Blocks until" not in by["T8"]["notes"] and "Records decision to audit trail." in by["T8"]["notes"]
    assert "operator_ask" in by["T8"]["notes"] and "@answer_handler" in by["T26"]["notes"]
    assert {c.split(":")[0] for c in changes} >= {"approval_made_asynchronous", "approval_flow_split"}
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


# ── §17.1774 the job's tables exist before its code runs against them (live job 918b6274's eighth plan) ──

def test_every_code_step_stands_on_the_jobs_revision():
    tasks = [
        {"id": "T2", "name": "Design audit trail schema", "tool": "LLM", "type": "decision", "depends_on": []},
        {"id": "T3", "name": "Implement audit trail schema", "tool": "CodeGen", "type": "action", "depends_on": ["T2"],
         "notes": "Create migration SQL for append-only tables."},
        {"id": "T4", "name": "Configure Alpaca paper client", "tool": "CodeGen", "type": "action", "depends_on": []},
        {"id": "T5", "name": "Implement risk controls", "tool": "CodeGen", "type": "action", "depends_on": [],
         "notes": "Max position 5% equity, max daily loss 3% halts new orders."},
        {"id": "T24", "name": "Validate risk controls", "tool": "LLM", "type": "validation", "depends_on": ["T5"]},
    ]
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert "T3" in by["T5"]["depends_on"] and "T3" in by["T4"]["depends_on"] and "T3" in by["WIRE"]["depends_on"]
    assert by["T3"]["depends_on"] == ["T2"]                                       # the revision stands on no code
    assert by["T24"]["depends_on"] == ["T5"]                                      # an LLM review is not code
    assert any(c.startswith("schema_first: T5") for c in changes)
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


# ── §17.1780 an outside API is researched before the code that calls it (live job f00ea97f) ──

PLAN_API = [
    {"id": "T3", "name": "Implement audit trail schema", "tool": "CodeGen", "type": "action", "depends_on": [],
     "notes": "Create migration SQL for append-only tables."},
    {"id": "T5", "name": "Implement Alpaca paper client", "tool": "CodeGen", "type": "action", "depends_on": ["T3"],
     "notes": "Python module `paper_alpaca_paper_client.py`. Reads API keys from the secrets store; REST + websocket."},
    {"id": "T7", "name": "Implement EDGAR Form 4 ingestion", "tool": "CodeGen", "type": "action", "depends_on": ["T3"],
     "notes": "Python module `paper_edgar_form_4_ingestion.py`. Insider purchases from Form 4 filings."},
    {"id": "T18", "name": "Implement fill tracker", "tool": "CodeGen", "type": "action", "depends_on": ["T5", "T3"],
     "notes": "Python module `paper_fill_tracker.py`. Consumes the paper client's stream of trade updates."},
    {"id": "T15", "name": "Implement trade proposer", "tool": "CodeGen", "type": "action", "depends_on": ["T7", "T3"],
     "notes": "Python module `paper_trade_proposer.py`. Weighs the stored signals."},
]


def test_each_outside_api_is_researched_and_its_callers_stand_on_it():
    tasks = copy.deepcopy(PLAN_API)
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    by = {t["id"]: t for t in tasks}
    assert by["API_ALPACA"]["tool"] == "SearXNG" and by["API_SEC_EDGAR"]["tool"] == "SearXNG"
    assert "exact field names and nesting" in by["API_SEC_EDGAR"]["notes"] and "cite its URL" in by["API_SEC_EDGAR"]["notes"]
    assert "API_ALPACA" in by["T5"]["depends_on"] and "API_SEC_EDGAR" in by["T7"]["depends_on"]
    assert "API_ALPACA" in by["T18"]["depends_on"]                     # consumes the client's stream: one hop
    assert not any(d.startswith("API_") for d in by["T15"]["depends_on"])   # downstream of an ingester only
    assert not any(k == "API_READS" for k in by)                       # "Reads API keys" is not an API
    assert by["API_ALPACA"]["depends_on"] == []                        # research stands on nothing (not the schema)
    assert any(c.startswith("api_research_added: API_SEC_EDGAR") for c in changes)
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


def test_a_named_api_outside_the_known_list_is_found_only_when_introduced_as_one():
    tasks = [{"id": "A", "name": "Implement quotes", "tool": "CodeGen", "type": "action", "depends_on": [],
              "notes": "Python module `paper_quotes.py`. Pulls daily bars via the Tiingo REST API."},
             {"id": "B", "name": "Implement limits", "tool": "CodeGen", "type": "action", "depends_on": [],
              "notes": "Python module `paper_limits.py`. Reads API keys. Uses SearXNG via the SearXNG API."}]
    engine_plan.complete_engine_plan(tasks, BRIEF)
    keys = {t["id"] for t in tasks}
    assert "API_TIINGO" in keys and "API_SEARXNG" not in keys and "API_READS" not in keys


@pytest.mark.integration
async def test_completion_is_applied_to_a_started_plan_without_touching_finished_steps():
    import uuid
    from app.database import async_session
    from app.modules import node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'blocked', "
                                  "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(BRIEF)})
            for i, (t, st) in enumerate(zip(PLAN_API, ["done", "done", "failed", "failed", "pending"], strict=True)):
                await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, tool, node_type, status, depends_on, "
                    "prompt_template, execution_order) VALUES (:j, :k, :t, :tool, 'task', :s, :d, :p, :o)"),
                    {"j": jid, "k": t["id"], "t": t["name"], "tool": t["tool"], "s": st, "d": t["depends_on"],
                     "p": t["notes"], "o": i})
            await db.commit()
        # make the stored plan already "completed" by earlier passes, as a real started plan is
        async with async_session() as db:
            out = await engine_plan.apply_to_job(db, jid, edited_by="test")
        assert "API_ALPACA" in out["inserted"] and "API_SEC_EDGAR" in out["inserted"]
        assert "T5" in out["kept_finished"] and "T5" not in out["edited"]              # done: never touched
        assert {"T7", "T18"} <= set(out["edited"])                                     # failed: updated (and reset)
        async with async_session() as db:
            rows = {r[0]: (r[1], list(r[2] or [])) for r in (await db.execute(text(
                "SELECT node_key, status, depends_on FROM dag_nodes WHERE job_id = :j"), {"j": jid})).all()}
        assert rows["T5"] == ("done", ["T3"])
        assert "API_SEC_EDGAR" in rows["T7"][1] and rows["API_SEC_EDGAR"][0] == "pending"
        async with async_session() as db:                                              # applying again: nothing new
            again = await engine_plan.apply_to_job(db, jid, edited_by="test")
        assert again["inserted"] == [] and again["edited"] == []
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


# ── §17.1797 a step planned before the scratch database is reworded (live job f00ea97f T25) ──

T25_STALE = ("Python module `paper_integrate_lab_scaffold_engine.py`. Wire all modules into the scaffold engine's existing "
             "code. In this same step also write `tests/test_paper_integrate_lab_scaffold_engine.py`: pytest tests of "
             "what `paper_integrate_lab_scaffold_engine.py` must do (its rules, its edge cases, its failure paths), with "
             "`async_session`, HTTP clients and model calls mocked -- they run before the step passes, with no network, "
             "database or secrets.")


def test_a_stale_no_database_test_sentence_is_replaced_once():
    tasks = [{"id": "T25", "name": "Integrate lab into scaffold engine", "tool": "CodeGen", "depends_on": [],
              "notes": T25_STALE},
             {"id": "T26", "name": "Wire it", "tool": "CodeGen", "depends_on": ["T25"],
              "notes": "Register paper_integrate_lab_scaffold_engine with get_scheduler() in app/main.py."}]
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    assert "module_tests_reworded: T25 tests use the scratch database" in changes
    notes = tasks[0]["notes"]
    assert "no network, database or secrets" not in notes and "mocked --" not in notes
    assert "`async_session` is a scratch database with this job's tables" in notes
    assert notes.count("tests/test_paper_integrate_lab_scaffold_engine.py") == 1
    assert notes.startswith("Python module `paper_integrate_lab_scaffold_engine.py`. Wire all modules")
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


# ── §17.1799 an integration step with several entry points is one step per entry point (live f00ea97f T25) ──

def _integration_plan():
    return [
        {"id": "T15", "name": "Implement approval gate", "tool": "CodeGen", "depends_on": [], "status": "done",
         "notes": "Python module `paper_operator_approval_gate.py`, with tests in `tests/test_paper_operator_approval_gate.py`."},
        {"id": "T24", "name": "Build operator dashboard", "tool": "CodeGen", "depends_on": [], "status": "skipped",
         "notes": "Python module `paper_operator_dashboard.py`, with tests in `tests/test_paper_operator_dashboard.py`."},
        {"id": "T25", "name": "Integrate lab into scaffold engine", "tool": "CodeGen", "depends_on": ["T15", "T24"],
         "status": "failed", "notes": T25_STALE.replace(
             "existing code.", "existing code. Expose as a lab module with entry points for daily batch proposals, "
             "approval, execution, backtesting, dashboard. This is the deliverable code module.")},
        {"id": "T26", "name": "Wire it", "tool": "CodeGen", "depends_on": ["T25"],
         "notes": "Register paper_integrate_lab_scaffold_engine with get_scheduler() in app/main.py."},
    ]


def test_each_entry_point_is_its_own_step_and_the_facade_reexports_them():
    tasks = _integration_plan()
    changes = engine_plan.complete_engine_plan(tasks, BRIEF)
    assert "entry_points_split: T25 → T25E1, T25E2, T25E3, T25E4 (left out: dashboard)" in changes
    by = {t["id"]: t for t in tasks}
    assert [by[k]["name"] for k in ("T25E1", "T25E2", "T25E3", "T25E4")] == [
        "Entry point: daily batch proposals", "Entry point: approval", "Entry point: execution",
        "Entry point: backtesting"]
    e3 = by["T25E3"]
    assert e3["depends_on"] == ["T15", "T24"] and e3["tool"] == "CodeGen"
    assert "`paper_entry_execution.py`" in e3["notes"] and "tests/test_paper_entry_execution.py" in e3["notes"]
    assert "never a placeholder" in e3["notes"]
    facade = by["T25"]
    assert facade["depends_on"] == ["T15", "T24", "T25E1", "T25E2", "T25E3", "T25E4"]
    assert "re-export the entry function of `paper_entry_daily_batch_proposals`" in facade["notes"]
    assert "Not exposed: dashboard (its step was skipped)." in facade["notes"]
    assert "entry points for" not in facade["notes"] and "no network, database or secrets" not in facade["notes"]
    assert [t["id"] for t in tasks].index("T25E1") < [t["id"] for t in tasks].index("T25")
    assert engine_plan.complete_engine_plan(tasks, BRIEF) == []


def test_two_entry_points_are_left_as_one_step():
    tasks = [{"id": "A", "name": "Build CLI", "tool": "CodeGen", "depends_on": [],
              "notes": "Python module `paper_cli.py` with entry points for run and stop, with tests in "
                       "`tests/test_paper_cli.py`. Register it with get_scheduler() in app/main.py."}]
    assert not any(c.startswith("entry_points_split") for c in engine_plan.complete_engine_plan(tasks, BRIEF))
    assert [t["id"] for t in tasks] == ["A"]


@pytest.mark.asyncio
async def test_apply_to_job_carries_status_so_a_skipped_step_is_left_out():
    rows = [{"node_key": t["id"], "title": t["name"], "prompt_template": t["notes"], "tool": t["tool"],
             "node_type": "task", "status": t.get("status", "pending"), "depends_on": t["depends_on"],
             "tool_config": None} for t in _integration_plan()]
    db = MagicMock()
    brief_row = MagicMock(); brief_row.mappings.return_value.first.return_value = {"refined_brief": BRIEF}
    plan_rows = MagicMock(); plan_rows.mappings.return_value.all.return_value = rows
    db.execute = AsyncMock(side_effect=[brief_row, plan_rows])
    with patch("app.modules.engine_source.targets_the_engine", return_value=True), \
         patch("app.modules.node_editor.insert_node", new=AsyncMock(return_value={"status": "ok"})) as ins, \
         patch("app.modules.node_editor.edit_node", new=AsyncMock(return_value={"status": "ok"})) as ed:
        out = await engine_plan.apply_to_job(db, str(uuid4()))
    assert out["inserted"] == ["T25E1", "T25E2", "T25E3", "T25E4"]
    assert "T25" in out["edited"] and "Not exposed: dashboard" in ed.await_args_list[
        [c.args[1] for c in ed.await_args_list].index("T25")].args[2]["prompt_template"]
    assert ins.await_count == 4
