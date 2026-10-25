"""§17.1818 — a review step's findings about the earlier job's modules become fix steps.

Live (follow-up 5b4880c0, 2026-10-25): T1 "Audit existing paper modules" found `paper_trade_proposer` returning
proposals with no `signal_id` and inserting rejected ones with NULL `signal_id` and quantity 0 -- both refused by the
schema -- plus defects in the P&L calculator, signal generator, news and Form 4 ingestion. Nothing in the plan fixed
them: the plan named only the brief's three bugs. T6, the daily batch built on the proposer, failed nine attempts on
those rows.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import engine_plan
from app.modules import execution_agent as ea

CARRIED = ["paper_trade_proposer", "paper_p_l_calculator", "paper_order_executor", "paper_spy_benchmark_tracker",
           "paper_alpaca_paper_client"]
REVIEW = """# Paper-Trading Lab Inventory

## 1. Ticker/symbol mixing

- `app/modules/paper_p_l_calculator.py` — `_fetch_fills` runs `SELECT f.ticker ...`. Migration 0010's `fill` table has no `ticker` column; the query fails at runtime.
- `paper_trade_proposer._record_rejected_proposal` inserts `signal_id = NULL` into `proposal`, but migration 0010 declares `proposal.signal_id` NOT NULL.
- `app/modules/paper_order_executor.py` — reads `proposal["symbol"]` while the proposal table and proposer use `ticker` for the same field.

## 2. api.example.com URL

- `app/modules/paper_spy_benchmark_tracker.py` — module constant `SPY_PRICE_URL` is a placeholder vendor URL that does not exist.

## 3. Twelve untested modules

- `app/modules/paper_trade_proposer.py`
- `app/modules/paper_alpaca_paper_client.py`
"""


def _plan():
    return [
        {"id": "T1", "name": "Audit existing paper modules", "tool": "LLM", "status": "done", "depends_on": [],
         "notes": "Read the existing paper_* modules."},
        {"id": "T2", "name": "Fix ticker field consistency", "tool": "CodeGen", "status": "done", "depends_on": ["T1"],
         "notes": "Python module `paper_order_executor.py` -- the earlier job's module: edit it in place."},
        {"id": "T3", "name": "Fix SPY benchmark tracker", "tool": "CodeGen", "status": "pending", "depends_on": ["T1"],
         "notes": "Python module `paper_spy_benchmark_tracker.py` -- the earlier job's module: edit it in place."},
        {"id": "T6", "name": "Build daily signal-to-proposal batch", "tool": "CodeGen", "status": "failed",
         "depends_on": ["T4"], "notes": "Python module `paper_daily_signal_proposal_batch.py`."},
        {"id": "T10", "name": "Are the keys stored?", "tool": "LLM", "status": "pending", "depends_on": ["T9"], "notes": ""},
    ]


def test_a_finding_is_a_bullet_about_a_module_not_a_bare_list_of_paths():
    found = engine_plan.review_findings(REVIEW, CARRIED)
    assert set(found) == {"paper_p_l_calculator", "paper_trade_proposer", "paper_order_executor",
                          "paper_spy_benchmark_tracker"}
    assert "paper_alpaca_paper_client" not in found                 # listed as untested: an inventory, no finding
    assert len(found["paper_trade_proposer"]) == 1 and "NOT NULL" in found["paper_trade_proposer"][0]


def test_each_unfixed_module_gets_a_fix_step_and_the_code_steps_wait_for_it():
    tasks = _plan()
    changes = engine_plan.add_review_fixes(tasks, {"T1": REVIEW}, CARRIED)
    new = {t["id"]: t for t in tasks if t["id"].startswith("T1F")}
    mods = {t["notes"].split("`")[1] for t in new.values()}
    assert mods == {"paper_p_l_calculator.py", "paper_trade_proposer.py"}          # T2, T3 already fix theirs
    fix = next(t for t in new.values() if "paper_trade_proposer" in t["notes"])
    assert fix["tool"] == "CodeGen" and fix["depends_on"] == ["T1"]
    assert fix["notes"].startswith("Python module `paper_trade_proposer.py` -- the earlier job's module: edit it in place")
    assert "inserts `signal_id = NULL`" in fix["notes"]                             # the review's own words
    assert "tests/test_paper_trade_proposer.py" in fix["notes"]
    by = {t["id"]: t for t in tasks}
    assert set(new) <= set(by["T6"]["depends_on"]) and set(new) <= set(by["T3"]["depends_on"])   # not yet passed
    assert by["T2"]["depends_on"] == ["T1"]                                         # a finished step is left alone
    assert by["T10"]["depends_on"] == ["T9"]                                        # not a code step
    assert len(changes) == 2 and all(c.startswith("review_fix_added: T1F") for c in changes)


def test_applying_again_adds_nothing():
    tasks = _plan()
    engine_plan.add_review_fixes(tasks, {"T1": REVIEW}, CARRIED)
    n = len(tasks)
    assert engine_plan.add_review_fixes(tasks, {"T1": REVIEW}, CARRIED) == [] and len(tasks) == n


def test_without_an_earlier_job_nothing_is_added():
    tasks = _plan()
    assert engine_plan.add_review_fixes(tasks, {"T1": REVIEW}, []) == [] and len(tasks) == 5


@pytest.mark.asyncio
async def test_plan_completion_reads_the_finished_reviews():
    rows = [{"node_key": t["id"], "title": t["name"], "prompt_template": t["notes"], "tool": t["tool"], "node_type": "task",
             "status": t["status"], "depends_on": t["depends_on"], "tool_config": None} for t in _plan()]
    brief_row = MagicMock(); brief_row.mappings.return_value.first.return_value = {"refined_brief": {"targets_engine": True}}
    plan_rows = MagicMock(); plan_rows.mappings.return_value.all.return_value = rows
    reviews = MagicMock(); reviews.all.return_value = [
        ("T1", REVIEW),
        ("T10", "- `paper_alpaca_paper_client` keys are not stored yet on the Machines page.")]   # not a review step
    db = MagicMock(); db.execute = AsyncMock(side_effect=[brief_row, plan_rows, reviews]); db.commit = AsyncMock()
    with patch("app.modules.engine_source.carried_modules", new=AsyncMock(return_value=CARRIED)), \
         patch("app.modules.node_editor.insert_node", new=AsyncMock(return_value={"status": "ok"})) as ins, \
         patch("app.modules.node_editor.edit_node", new=AsyncMock(return_value={"status": "ok"})), \
         patch.object(engine_plan, "complete_engine_plan", return_value=[]) as cep:
        out = await engine_plan.apply_to_job(db, "5b4880c0-0000-4000-8000-000000000000", only_reviews=True)
    assert sorted(out["inserted"]) == ["T1F1", "T1F2"], out
    cep.assert_not_called()                                                         # only the review fixes
    assert {c.args[1]["node_key"] for c in ins.await_args_list} == {"T1F1", "T1F2"}


# ── the run loop: a finished review adds its fix steps at once ─────────────────────────────────────────────

async def _loop(title):
    peek = AsyncMock(side_effect=[{"node_key": "T1", "title": title, "tool": "LLM", "node_type": "task", "depends_on": []},
                                  None])
    exec_next = AsyncMock(side_effect=[{"status": "done", "node_key": "T1", "title": title, "tool": "LLM", "output": "x"},
                                       {"status": "complete", "total_nodes": 1, "passed": 1, "failed": 0}])
    from tests._execution_agent_shared import _make_sse_db
    _db_, mock_session = _make_sse_db(dag_node_count=1)
    apply = AsyncMock(return_value={"inserted": ["T1F1"]})
    with patch.object(settings, "decision_pause_enabled", False), \
         patch.object(settings, "parallel_execution_enabled", False), \
         patch.object(settings, "hands_on_assist_gate_enabled", False), \
         patch("app.modules.execution_agent.async_session", mock_session), \
         patch("app.modules.execution_agent._get_job", AsyncMock(return_value={"status": "executing", "id": "j"})), \
         patch("app.modules.execution_agent._peek_next_node", peek), \
         patch("app.modules.execution_agent.execute_next_node", exec_next), \
         patch("app.modules.execution_agent._make_dag_progress_tracker", AsyncMock(return_value=None)), \
         patch.object(engine_plan, "apply_to_job", new=apply):
        [c async for c in ea.execute_all_nodes("j")]
    return apply


@pytest.mark.asyncio
async def test_a_finished_review_step_adds_its_fix_steps():
    apply = await _loop("Audit existing paper modules")
    apply.assert_awaited_once()
    assert apply.await_args.kwargs["only_reviews"] is True and apply.await_args.args[1] == "j"


@pytest.mark.asyncio
async def test_another_finished_step_does_not():
    apply = await _loop("Document the lab")
    apply.assert_not_awaited()


# ── §17.1818b — re-applying plan completion changes nothing (the title is kept) ─────────────────────────────

async def _apply(rows):
    brief_row = MagicMock(); brief_row.mappings.return_value.first.return_value = {
        "refined_brief": {"title": "Finish paper-trading lab", "targets_engine": True}}
    plan_rows = MagicMock(); plan_rows.mappings.return_value.all.return_value = rows
    db = MagicMock(); db.execute = AsyncMock(side_effect=[brief_row, plan_rows]); db.commit = AsyncMock()
    with patch("app.modules.engine_source.carried_modules", new=AsyncMock(return_value=[])), \
         patch("app.modules.node_editor.insert_node", new=AsyncMock(return_value={"status": "ok"})), \
         patch("app.modules.node_editor.edit_node", new=AsyncMock(return_value={"status": "ok"})) as ed:
        out = await engine_plan.apply_to_job(db, "5b4880c0-0000-4000-8000-000000000000")
    return out, {c.args[1]: c.args[2] for c in ed.await_args_list}


@pytest.mark.asyncio
async def test_reapplying_plan_completion_is_a_no_op():
    t10 = {"node_key": "T10", "title": "Run end-to-end paper trade", "tool": "Shell", "node_type": "task",
           "status": "pending", "depends_on": [], "tool_config": None,
           "prompt_template": "Execute one trade on the paper account. Use secrets-store keys ALPACA_API_KEY_ID and "
                              "ALPACA_API_SECRET_KEY."}
    _, edits = await _apply([t10])
    first = edits["T10"]
    assert first["title"].startswith("Are the ") and first["tool"] == "LLM"           # the title is written too
    stored = {**t10, "title": first["title"], "tool": first["tool"], "prompt_template": first["prompt_template"],
              "depends_on": first.get("depends_on", t10["depends_on"])}
    _, again = await _apply([stored])
    assert "T10" not in again, again["T10"]                                            # live: it became CodeGen
