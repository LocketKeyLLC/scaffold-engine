"""§17.1950 — a model the planner writes on a step is its own guess, not the operator's choice: dropped, so the step
runs on its role's model. Live (2026-10-15 → 11-05): the planner (deepseek-v4-pro) pinned its own tag on 68 CodeGen
steps of four jobs; each bypassed `model_coder` (§17.427) and drew on the account's costliest model."""
from __future__ import annotations

from app.modules import dag_generator as dg


def _task(tool: str, model) -> dict:
    return {"id": "T1", "name": "Write the module", "type": "action", "inputs": [], "outputs": ["module"],
            "depends_on": [], "tool": tool, "assigned_model": model}


def test_a_planner_pin_on_any_step_is_dropped():
    for tool in ("CodeGen", "LLM", "SearXNG"):
        tasks, errors, _w = dg._normalize_tasks([_task(tool, "deepseek-v4-pro:cloud")])
        assert not errors and "assigned_model" not in tasks[0], tool


def test_a_null_pin_stays_absent_and_the_schema_asks_for_none():
    tasks, _e, _w = dg._normalize_tasks([_task("CodeGen", None)])
    assert "assigned_model" not in tasks[0]
    assert '"assigned_model": null,' in dg.DAG_SYSTEM and "model name or null" not in dg.DAG_SYSTEM
