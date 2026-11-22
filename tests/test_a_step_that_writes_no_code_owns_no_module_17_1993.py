"""§17.1993 — only a step that writes code owns a module, and a finished code step keeps the name the plan gives it.

Live (job 69fe61c4 "Paper-Trading Lab Operator Dashboard", 2026-11-22 ~01:40 UTC): T8 "Add dashboard route" was refused
only for "`app.modules.paper_dashboard`: that module is step T2's and is not written yet". T2 is a Milvus read-only
survey ("No edits") whose output suggested a block headed `# app/modules/paper_dashboard.py`; `step_module` took that
header, so the survey owned a module (and `files_of` would have delivered its sketch). T5, the CodeGen step that
actually wrote the metrics module, named no module: complete-plan had named it at 21:55:24, but T5 was done, the name
was kept out, and the operator reset T5 13 s later -- it ran with no name and called itself `app.modules.paper_dashboard`
in its docstring. T6 was then told the module was T2's and copied T5's math locally. The rows below are cut down
from that job's stored outputs (the lab's code shortened; names and shapes as stored).
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import engine_plan, engine_source
from app.sandbox.engine_tests import files_of

T2_SURVEY = (
    "Findings from the read-only survey:\n\n"
    "- Lab routes are in `app/modules/paper_wire_engine_entry_module.py` under `/labs/paper`.\n\n"
    "Add one new module `app/modules/paper_dashboard.py` and register its routes on the existing router.\n\n"
    "```python\n# app/modules/paper_dashboard.py\n"            # live: a sketch with placeholders -- it does not parse
    "from app.modules.paper_wire_engine_entry_module import <EXISTING_BLUEPRINT_NAME> as paper_bp\n\n\n"
    "@paper_bp.route('/dashboard')\ndef paper_dashboard():\n    return render_template('paper_dashboard.html')\n```\n")
T5_OUT = (
    '```python\n"""app.modules.paper_dashboard — read-only series math for /labs/paper/dashboard.\n\nPure functions."""\n'
    "from __future__ import annotations\n\n\ndef span_filter(points, span):\n    return list(points)\n\n\n"
    "def compute_series(view, spy, span):\n    return {'points': span_filter(view.get('points', []), span)}\n```\n")
T6_OUT = (
    '```python\n"""app.modules.paper_dashboard_context — template context builder.\n\n'
    'Computes the aligned series locally (no import from app.modules.paper_dashboard)."""\n\n\n'
    "def build_dashboard_context(view, spy, span):\n    return {'series': []}\n```\n")
T8_OUT = (
    "```python\n# app/modules/paper_wire_engine_entry_module.py\nfrom fastapi import APIRouter\n\n"
    "from app.modules.paper_dashboard import compute_series\n\nrouter = APIRouter()\n\n\n"
    "@router.get('/labs/paper/dashboard')\nasync def dashboard(span: str = '1m'):\n"
    "    return compute_series({}, [], span)\n```\n")
T12_ASK = ("The engine exports this job's code (`GET /jobs/{id}/code`); `scripts/deliver_job.py <job>` writes them onto a "
           "branch. Is it merged and running?")


def _rows(t5_prompt: str = "Pure read-only computation from existing data. Exports compute_series(view, spy, span).",
          t5_status: str = "done") -> list[dict]:
    return [
        {"node_key": "T2", "tool": "Milvus", "status": "done", "title": "Inspect existing lab modules",
         "prompt_template": "Read-only survey of the existing lab code. No edits.", "output_text": T2_SURVEY},
        {"node_key": "T5", "tool": "CodeGen", "status": t5_status, "title": "Write metrics computation module",
         "prompt_template": t5_prompt, "output_text": T5_OUT if t5_status == "done" else None},
        {"node_key": "T6", "tool": "CodeGen", "status": "done", "title": "Write dashboard view builder",
         "prompt_template": "Reshapes existing data into template context. Imports compute_series from T5.",
         "output_text": T6_OUT},
        {"node_key": "T8", "tool": "CodeGen", "status": "failed", "title": "Add dashboard route",
         "prompt_template": "Python module `paper_wire_engine_entry_module.py` -- the engine's own: edit it in place.",
         "tool_config": {"fixes_module": "paper_wire_engine_entry_module"}, "output_text": T8_OUT},
        {"node_key": "T12", "tool": "LLM", "status": "pending",
         "title": "Is this job's code merged into the engine and running?", "prompt_template": T12_ASK},
    ]


def test_a_survey_or_a_delivery_question_owns_no_module_and_delivers_no_file():
    by = {r["node_key"]: r for r in _rows()}
    assert engine_source.step_module(by["T2"]) == ""                      # live: 'paper_dashboard'
    assert engine_source.step_module(by["T12"]) == ""                     # live: 'deliver_job'
    assert engine_source.step_module_path(by["T2"]) == ""
    assert files_of(by["T2"]) == {}                                       # live: app/modules/paper_dashboard.py
    assert "paper_dashboard" not in engine_source.sibling_definitions(list(by.values()))
    # correct misses: a code step keeps its header's module; a row whose tool is not known (a partial dict) still owns
    assert engine_source.step_module(by["T8"]) == "paper_wire_engine_entry_module"
    assert engine_source.step_module({"output_text": T2_SURVEY}) == "paper_dashboard"
    assert files_of(by["T8"]) and "app/modules/paper_wire_engine_entry_module.py" in files_of(by["T8"])


def test_the_dashboard_route_is_not_refused_for_a_survey_s_module():
    rows = _rows()
    t8 = next(r for r in rows if r["node_key"] == "T8")
    others = [r for r in rows if r["node_key"] != "T8"]
    done = [r for r in others if r["status"] == "done"]
    found = engine_source.imports_an_unwritten_sibling(t8["output_text"], others, engine_source.sibling_definitions(done))
    assert not any("step T2's" in f for f in found), found


def test_a_code_step_s_unwritten_module_is_still_refused_as_its_own():
    # the gate still bites for the step that really writes the module: T5 named by the plan, not yet run
    rows = _rows(t5_prompt="Python module `paper_metrics_computation_module.py`. Exports compute_series(view, spy, span).",
                 t5_status="pending")
    out = T8_OUT.replace("app.modules.paper_dashboard", "app.modules.paper_metrics_computation_module")
    others = [r for r in rows if r["node_key"] != "T8"]
    done = [r for r in others if r["status"] == "done"]
    found = engine_source.imports_an_unwritten_sibling(out, others, engine_source.sibling_definitions(done))
    assert any("is step T5's and is not written yet" in f for f in found), found


def test_a_step_s_code_that_calls_itself_another_module_is_refused():
    rows = _rows(t5_prompt="Python module `paper_metrics_computation_module.py`. Exports compute_series(view, spy, span).")
    t5 = next(r for r in rows if r["node_key"] == "T5")
    others = [r for r in rows if r["node_key"] != "T5"]
    assert engine_source.step_module(t5) == "paper_metrics_computation_module"
    found = engine_source.calls_itself_another_module(T5_OUT, t5, others)
    assert found and "`app/modules/paper_metrics_computation_module.py`" in found[0], found
    # correct misses: the docstring names its own module; the block is headed (its header decides); no planned name
    ok = T5_OUT.replace("app.modules.paper_dashboard", "app.modules.paper_metrics_computation_module")
    assert engine_source.calls_itself_another_module(ok, t5, others) == []
    headed = T5_OUT.replace("```python\n", "```python\n# app/modules/paper_metrics_computation_module.py\n", 1)
    assert engine_source.calls_itself_another_module(headed, t5, others) == []
    unnamed = {**t5, "prompt_template": "Pure read-only computation from existing data."}
    assert engine_source.calls_itself_another_module(T5_OUT, unnamed, others) == []


BRIEF = {"title": "Paper-Trading Lab Operator Dashboard",
         "description": "Add a read-only operator dashboard to the paper-trading lab in the scaffold engine's own codebase.",
         "goals": ["Add a dashboard module to this repository's code."]}


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_code_rows_carry_the_tool_and_a_finished_step_keeps_its_planned_name():
    from sqlalchemy import text

    from app.database import async_session
    jid = str(uuid.uuid4())
    rows = _rows()
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'dash', 'blocked', "
                                  "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(BRIEF)})
            for i, r in enumerate(rows):
                await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, tool, node_type, status, depends_on, prompt_template,"
                    " output_text, tool_config, execution_order) VALUES (:j, :k, :t, :tool, 'task', :s, :d, :p, :o,"
                    " CAST(:c AS jsonb), :n)"),
                    {"j": jid, "k": r["node_key"], "t": r["title"], "tool": r["tool"], "s": r["status"],
                     "d": [], "p": r["prompt_template"], "o": r.get("output_text"),
                     "c": json.dumps(r["tool_config"]) if r.get("tool_config") else None, "n": i})
            await db.commit()
        async with async_session() as db:
            crows = await engine_source.code_rows(db, jid)
            mods, _names = await engine_source.job_code(db, jid, except_node="T8")
        assert {r["node_key"]: engine_source.step_module(r) for r in crows}["T2"] == ""
        assert {r["node_key"]: r.get("tool") for r in crows}["T2"] == "Milvus"
        assert "paper_dashboard.py" not in mods                       # the survey's sketch is no sibling
        assert "app/modules/paper_dashboard.py" not in engine_source.job_files(crows)

        async with async_session() as db:
            out = await engine_plan.apply_to_job(db, jid, edited_by="test")
        assert "T5" in out["named_finished"], out
        async with async_session() as db:
            t5 = dict((await db.execute(text(
                "SELECT status, prompt_template, output_text, tool FROM dag_nodes WHERE job_id = :j AND node_key = 'T5'"),
                {"j": jid})).mappings().first())
            audit = (await db.execute(text(
                "SELECT count(*) FROM dag_node_edits WHERE job_id = :j AND node_key = 'T5' AND op = 'edit'"),
                {"j": jid})).scalar()
            crows = await engine_source.code_rows(db, jid)
        assert t5["status"] == "done" and t5["output_text"] == T5_OUT        # named, not reset
        own = engine_source.step_module(t5)
        assert own and own != "paper_dashboard", t5["prompt_template"]
        assert audit == 1
        assert f"app/modules/{own}.py" in engine_source.job_files(crows)      # delivered under the planned name
        async with async_session() as db:                                    # applying again: nothing new
            again = await engine_plan.apply_to_job(db, jid, edited_by="test")
        assert again["named_finished"] == []
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
