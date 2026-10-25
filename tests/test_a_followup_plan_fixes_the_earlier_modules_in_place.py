"""§17.1814 — a follow-up job's plan fixes the earlier job's modules in place and names its new ones alike.

Live (follow-up 5b4880c0, the plan completion previewed 2026-10-25): with no knowledge of f00ea97f's modules the pass
named T2 "Edit paper_order_executor in place" a NEW `finish_fix_ticker_field_consistency.py` and T3 "Fix SPY benchmark
tracker" `finish_fix_spy_benchmark_tracker.py` -- the "new module under another name" the brief forbids -- and took
the prefix `finish` from the new brief's title ("Finish paper-trading research lab ...") instead of the lab's `paper`.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import engine_plan, engine_source

CARRIED = ["paper_alpaca_paper_client", "paper_spy_benchmark_tracker", "paper_order_executor"]
BRIEF = {"title": "Finish paper-trading research lab in scaffold engine", "targets_engine": True}


def _plan():
    return [
        {"id": "T2", "name": "Fix ticker field consistency", "tool": "CodeGen", "depends_on": [],
         "notes": "Edit paper_order_executor in place. Choose one field name."},
        {"id": "T3", "name": "Fix SPY benchmark tracker", "tool": "CodeGen", "depends_on": [],
         "notes": "Replace the api.example.com URL with the existing Alpaca paper client's market-data endpoint."},
        {"id": "T7", "name": "Build execution entry point", "tool": "CodeGen", "depends_on": [],
         "notes": "Send the approved order via the Alpaca paper client and record the fill."},
    ]


def _notes(tasks):
    return {t["id"]: t["notes"] for t in tasks}


def test_a_step_that_fixes_an_earlier_module_writes_it_in_place():
    tasks = _plan()
    changes = engine_plan.complete_engine_plan(tasks, BRIEF, CARRIED)
    notes = _notes(tasks)
    assert notes["T2"].startswith("Python module `paper_order_executor.py` -- the earlier job's module: edit it in place")
    assert notes["T3"].startswith("Python module `paper_spy_benchmark_tracker.py` -- the earlier job's module")
    assert "carried_module_named: T2 edits `paper_order_executor.py` in place (the earlier job wrote it)" in changes
    assert "tests/test_paper_order_executor.py" in notes["T2"]                        # and it gets its tests


def test_a_module_the_step_only_uses_is_not_its_own_and_new_modules_take_the_lab_prefix():
    tasks = _plan()
    engine_plan.complete_engine_plan(tasks, BRIEF, CARRIED)
    t7 = _notes(tasks)["T7"]
    assert t7.startswith("Python module `paper_execution_entry_point.py`.")       # not the client it sends through
    assert "finish_" not in " ".join(_notes(tasks).values())


def test_without_an_earlier_job_the_plan_is_named_as_before():
    tasks = _plan()
    engine_plan.complete_engine_plan(tasks, BRIEF)
    notes = _notes(tasks)
    assert notes["T2"].startswith("Python module `finish_fix_ticker_field_consistency.py`.")
    assert "edit it in place" not in " ".join(notes.values())


def test_a_new_module_never_takes_an_earlier_modules_name():
    tasks = [{"id": "T9", "name": "Implement order executor", "tool": "CodeGen", "depends_on": [], "notes": "x"}]
    engine_plan.complete_engine_plan(tasks, {"title": "Paper lab"}, ["paper_risk"])
    assert tasks[0]["notes"].startswith("Python module `paper_order_executor.py`.")
    tasks = [{"id": "T9", "name": "Implement order executor", "tool": "CodeGen", "depends_on": [], "notes": "x"}]
    engine_plan.complete_engine_plan(tasks, {"title": "Paper lab"}, ["paper_risk", "paper_order_executor_old"])
    assert "paper_order_executor.py" in tasks[0]["notes"]
    tasks = [{"id": "T9", "name": "Implement executor", "tool": "CodeGen", "depends_on": [], "notes": "x"}]
    engine_plan.complete_engine_plan(tasks, {"title": "Paper lab"}, ["paper_executor"])   # one word: not "names" it
    assert tasks[0]["notes"].startswith("Python module `paper_executor_2.py`.")          # so never its name
    assert engine_plan._carried_prefix(["paper_a", "paper_b", "lab_c"]) == "paper"
    assert engine_plan._carried_prefix([]) is None


def test_the_word_edit_must_name_the_module():
    t = {"id": "T", "name": "Build execution", "tool": "CodeGen",
         "notes": "Update the audit trail after sending through paper_order_executor."}
    assert engine_plan._carried_own(t, CARRIED) is None
    t["notes"] = "Fix the existing `paper_order_executor` so it reads symbol."
    assert engine_plan._carried_own(t, CARRIED) == "paper_order_executor"


@pytest.mark.asyncio
async def test_the_earlier_jobs_modules_are_read_in_plan_order():
    rows = [{"node_key": "f00ea97f:T5", "prompt_template": "Python module `paper_alpaca_paper_client.py`",
             "output_text": "", "title": "", "description": "", "status": "done"},
            {"node_key": "f00ea97f:T1", "prompt_template": "", "output_text": "a decision", "title": "Decide",
             "description": "", "status": "done"},
            {"node_key": "T2", "prompt_template": "Python module `mine.py`", "output_text": "", "title": "",
             "description": "", "status": "pending"}]
    with patch.object(engine_source, "code_rows", new=AsyncMock(return_value=rows)):
        assert await engine_source.carried_modules(MagicMock(), "j") == ["paper_alpaca_paper_client"]


# ── the production paths ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_planner_hands_the_pass_the_earlier_jobs_modules():
    from tests.test_an_engine_plan_is_complete import _generate
    spy = MagicMock(wraps=engine_plan.complete_engine_plan)
    with patch.object(engine_source, "carried_modules", new=AsyncMock(return_value=CARRIED)), \
         patch.object(engine_plan, "complete_engine_plan", new=spy):
        await _generate(BRIEF)
    assert spy.call_args.args[2] == CARRIED


@pytest.mark.asyncio
async def test_plan_completion_on_a_started_job_hands_the_pass_the_earlier_jobs_modules():
    brief_row = MagicMock(); brief_row.mappings.return_value.first.return_value = {"refined_brief": BRIEF}
    plan_rows = MagicMock(); plan_rows.mappings.return_value.all.return_value = [
        {"node_key": t["id"], "title": t["name"], "prompt_template": t["notes"], "tool": t["tool"], "node_type": "task",
         "status": "pending", "depends_on": [], "tool_config": None} for t in _plan()]
    db = MagicMock(); db.execute = AsyncMock(side_effect=[brief_row, plan_rows]); db.commit = AsyncMock()
    with patch.object(engine_source, "carried_modules", new=AsyncMock(return_value=CARRIED)), \
         patch("app.modules.node_editor.insert_node", new=AsyncMock(return_value={"status": "ok"})), \
         patch("app.modules.node_editor.edit_node", new=AsyncMock(return_value={"status": "ok"})) as ed:
        await engine_plan.apply_to_job(db, "5b4880c0-0000-4000-8000-000000000000")
    t2 = next(c.args[2] for c in ed.await_args_list if c.args[1] == "T2")
    assert t2["prompt_template"].startswith("Python module `paper_order_executor.py` -- the earlier job's module")


def test_a_step_with_no_module_is_told_to_name_its_own():
    me = {"node_key": "T6", "title": "Build daily signal-to-proposal batch", "prompt_template": "Implement the batch."}
    out = "```python\n# app/modules/paper_entry_batch.py\nX = 1\n```"
    msg = engine_source.writes_a_siblings_module(out, me, [{"node_key": "T4", "title": "",
                                                            "prompt_template": "Python module `paper_entry_batch.py`"}])
    assert msg and "this step writes `.py`" not in msg[0] and "names no module of its own" in msg[0]
