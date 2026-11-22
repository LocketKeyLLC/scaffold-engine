"""§17.2000 — a module nothing reaches is not done: every module a job writes for the engine is imported, transitively,
from the lab's entry (or the engine's own code) -- planned so, checked on the step that makes it so, and checked again
by the delivery.

Live (69fe61c4 "Paper-Trading Lab Operator Dashboard", 2026-11-22): T9 "Serve static asset under router" wrote
`paper_serve_static_asset_under` (the StaticFiles mount at /labs/paper/static the page's Chart.js loads from) and T10
"Enforce read-only data access" wrote `paper_enforce_read_only_data` (a ReadOnlySession "on the dashboard's query
path"). Both were planned as new modules that "start from" T8, the step that edits the lab's entry
`paper_wire_engine_entry_module`, and T9 was told "no route or template edits": the entry could never import them.
Each passed its own tests; nothing imported either; the delivered page's script would 404 and the route read through
a plain session."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app.modules import engine_plan as ep
from app.modules import engine_source as es
from app.modules.module_reach import imported_modules, reached

REPO = Path(__file__).resolve().parent.parent
ENTRY = "paper_wire_engine_entry_module"
STATIC, READONLY = "paper_serve_static_asset_under", "paper_enforce_read_only_data"
METRICS, VIEW = "paper_metrics_computation_module", "paper_dashboard_view_builder"

BRIEF = {
    "title": "Paper-Trading Lab Operator Dashboard",
    "description": "Add a read-only operator dashboard to the paper-trading lab already living in the engine.",
    "goals": ["Add an HTML dashboard route at /labs/paper/dashboard inside the existing /labs/paper router in "
              f"app/modules/{ENTRY}.py, gated by the same API-key auth as the lab's other routes."],
}


def _t(i, name, tool, deps, notes, typ="action", **kw):
    return {"id": i, "name": name, "tool": tool, "type": typ, "depends_on": deps, "inputs": [], "outputs": [],
            "notes": notes, **kw}


def _plan() -> list[dict]:
    """The job's code steps and the steps after them, as stored (text shortened, shape kept)."""
    return [
        _t("T1", "Choose charting asset delivery", "LLM", [], "Operator decision.", "decision"),
        _t("T3", "Pin metric definitions", "LLM", [], "Text decision only.", "decision"),
        _t("T4", "Vendor charting asset", "CodeGen", ["T1"], ep._STATIC_LEAD + ": `app/static/paper/vendor.json` -- a "
           "JSON object naming each third-party file. Starts from T1. Only the asset acquisition."),
        _t("T5", "Write metrics computation module", "CodeGen", ["T3"], f"Python module `{METRICS}.py`. Pure "
           "read-only computation from existing data. Exports compute_series(view, spy, span). " + ep._tests_note(METRICS)),
        _t("T6", "Write dashboard view builder", "CodeGen", ["T5"], f"Python module `{VIEW}.py`. Reshapes existing "
           "view/audit/proposal data into template context. Imports compute_series from T5. " + ep._tests_note(VIEW)),
        _t("T7", "Write dashboard HTML template", "CodeGen", ["T4", "T6"], ep._STATIC_LEAD + ": "
           "`app/templates/paper/dashboard.html`. Template renders context from T6. No route logic."),
        _t("T8", "Add dashboard route", "CodeGen", ["T6", "T7"], f"Python module `{ENTRY}.py` -- the engine's own, "
           "which this job extends: edit it in place (the whole file, every existing public name, route and its auth "
           f"kept). Thin route in {ENTRY}.py: imports build_context (T6) and renders dashboard.html (T7). "
           + ep._tests_note(ENTRY), tool_config={"fixes_module": ENTRY}),
        _t("T9", "Serve static asset under router", "CodeGen", ["T4", "T8"], f"Python module `{STATIC}.py`. Starts "
           "from T4 + T8. Wires the lab's static serving so the pinned bundle is reachable under the router and CSP "
           "permits it. Only static serving — no route or template edits. " + ep._tests_note(STATIC)),
        _t("T10", "Enforce read-only data access", "CodeGen", ["T5", "T6", "T8"], f"Python module `{READONLY}.py`. "
           "Structurally enforce read-only (read-only DB role or read-only transaction) on the dashboard's query path "
           "so no helper can mutate lab data. No schema changes. " + ep._tests_note(READONLY)),
        _t("T11", "Verify existing routes unchanged", "LLM", ["T8"], "No edits.", "validation"),
        _t("T12", "Is this job's code merged into the engine and running?", "LLM", ["T8", "T9", "T10", "T4", "T5",
           "T6", "T7"], ep._DELIVER_NOTE, "decision"),
        _t("T15", "Verify read-only and auth", "LLM", ["T12", "T10"], "Checks the guard holds.", "validation"),
    ]


@pytest.fixture(autouse=True)
def _the_labs_router_is_the_engines(monkeypatch):
    """The lab 5b4880c0 delivered is in the engine (conftest hides delivered labs from the engine's own tests)."""
    index = {**es.module_index(), f"app.modules.{ENTRY}": {"doc": "", "names": {"install"}}}
    monkeypatch.setattr(ep, "module_index", lambda: index)


# ── what reaches what ────────────────────────────────────────────────────────────────────────────────────────────

def test_imports_are_read_in_every_form_and_reach_is_transitive():
    code = ("import app.modules.a\nfrom app.modules import b, c\nfrom app.modules.d import x\nfrom . import e\n"
            "from .f import y\nimport importlib\nimportlib.import_module('app.modules.g')\nfrom app.config import s\n"
            "def late():\n    from app.modules.h import z\n")
    assert imported_modules(code) == set("abcdefgh")
    assert imported_modules(code, None) == set("abcdgh")            # a relative import outside app/modules is not
    assert imported_modules("def (:") == set()
    codes = {ENTRY: f"from app.modules.{VIEW} import build_context\n", VIEW: f"from app.modules import {METRICS}\n",
             METRICS: "X = 1\n", STATIC: "Y = 1\n"}
    assert reached(codes, {ENTRY}) == {ENTRY, VIEW, METRICS}


# ── the plan end ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_live_plan_gets_a_step_that_makes_the_entry_use_t9_and_t10():
    tasks = _plan()
    changes = ep.reach_the_entry(tasks, {ENTRY}, [], {ENTRY})
    by = {t["id"]: t for t in tasks}
    assert changes and changes[0].startswith("entry_reaches: REACH")
    r = by["REACH"]
    assert r["tool"] == "CodeGen" and r["tool_config"] == {"fixes_module": ENTRY, "reaches": [STATIC, READONLY]}
    assert r["depends_on"] == ["T8", "T9", "T10"]
    assert r["notes"].startswith(f"Python module `{ENTRY}.py` -- the engine's own") and "edit it in place" in r["notes"]
    assert f"tests/test_{ENTRY}.py" in r["notes"] and "T9 Serve static asset" in r["notes"]
    assert es.step_module({"prompt_template": r["notes"], "title": r["name"], "tool": "CodeGen"}) == ENTRY
    # the checks and the delivery that waited on T9/T10 wait on their use; a finished check is left alone
    assert "REACH" in by["T12"]["depends_on"] and "REACH" in by["T15"]["depends_on"]
    assert "REACH" not in by["T11"]["depends_on"]
    # the correct misses: T5 and T6 are what the entry stands on; T4/T7 write no module
    assert METRICS not in r["tool_config"]["reaches"] and VIEW not in r["tool_config"]["reaches"]


def test_the_whole_completion_adds_it_once_and_a_second_pass_changes_nothing():
    tasks = _plan()
    changes = ep.complete_engine_plan(tasks, BRIEF, [])
    assert [c for c in changes if c.startswith("entry_reaches")], changes
    assert sum(t["id"].startswith("REACH") for t in tasks) == 1
    deliver = next(t for t in tasks if ep._DELIVER_QUESTION_RE.match(t["name"]))
    assert "REACH" in deliver["depends_on"]
    snapshot = json.dumps(tasks, sort_keys=True, default=str)
    assert ep.complete_engine_plan(tasks, BRIEF, []) == []
    assert json.dumps(tasks, sort_keys=True, default=str) == snapshot


def test_a_module_the_entry_stands_on_needs_no_step():
    """The correct miss: T9 and T10 planned before the entry's step, which stands on them."""
    tasks = _plan()
    by = {t["id"]: t for t in tasks}
    by["T9"]["depends_on"], by["T10"]["depends_on"] = ["T4"], ["T5", "T6"]
    by["T8"]["depends_on"] = ["T6", "T7", "T9", "T10"]
    assert ep.reach_the_entry(tasks, {ENTRY}, [], {ENTRY}) == []
    assert not any(t["id"] == "REACH" for t in tasks)


def test_a_plan_with_no_entry_is_left_to_the_wiring_step():
    tasks = [t for t in _plan() if t["id"] != "T8"]
    assert ep.reach_the_entry(tasks, set(), [], set()) == []


# ── the repair of a running job (complete-plan reads the finished code) ──────────────────────────────────────────

def _done(tasks):
    for t in tasks:
        t["status"] = "pending" if t["id"] in ("T12", "T15") else "done"
    return tasks


SOURCES = {ENTRY: f"from app.modules.{VIEW} import build_context\n\ndef install(app):\n    pass\n",
           VIEW: f"from app.modules.{METRICS} import compute_series\n", METRICS: "def compute_series(): ...\n",
           STATIC: "def install(app):\n    pass\n", READONLY: "class ReadOnlySession: ...\n"}


def test_the_live_job_finished_is_repaired_from_its_code():
    tasks = _done(_plan())
    changes = ep.reach_the_entry(tasks, {ENTRY}, [], {ENTRY}, SOURCES)
    r = next(t for t in tasks if t["id"] == "REACH")
    assert r["tool_config"]["reaches"] == [STATIC, READONLY] and changes
    assert "REACH" in next(t for t in tasks if t["id"] == "T12")["depends_on"]


def test_a_finished_entry_that_imports_them_needs_no_step():
    """The correct miss: the entry's code imports both -- even a module upstream of it the plan cannot see."""
    tasks = _done(_plan())
    src = {**SOURCES, ENTRY: SOURCES[ENTRY] + f"from app.modules import {STATIC}\nfrom app.modules.{READONLY} import "
                                              "ReadOnlySession\n"}
    assert ep.reach_the_entry(tasks, {ENTRY}, [], {ENTRY}, src) == []


def test_a_finished_module_upstream_of_the_entry_its_code_leaves_out_is_found():
    """The plan said the entry stands on T6; its finished code does not import it."""
    tasks = _done(_plan())
    src = {**SOURCES, ENTRY: f"from app.modules import {STATIC}, {READONLY}\n"}
    ep.reach_the_entry(tasks, {ENTRY}, [], {ENTRY}, src)
    r = next(t for t in tasks if t["id"] == "REACH")
    assert r["tool_config"]["reaches"] == [METRICS, VIEW]
    assert "the entry's code does not import them" in r["notes"]


def test_a_finished_job_whose_entry_code_is_not_read_is_not_judged_by_the_plan():
    """No code to read (a replay without sources): a finished step's module is a fact, not a plan -- no step."""
    tasks = _done(_plan())
    assert ep.reach_the_entry(tasks, {ENTRY}, [], {ENTRY}) == []


# ── the step end ─────────────────────────────────────────────────────────────────────────────────────────────────

ROW = {"node_key": "REACH", "tool": "CodeGen",
       "tool_config": json.dumps({"fixes_module": ENTRY, "reaches": [STATIC, READONLY]})}


def test_the_step_that_must_use_them_is_refused_until_it_imports_each():
    inline = {f"app/modules/{ENTRY}.py": "from fastapi.staticfiles import StaticFiles\n\ndef install(app):\n"
                                         "    app.mount('/labs/paper/static', StaticFiles(directory='x'))\n"}
    found = es.entry_leaves_out(inline, ROW)
    assert len(found) == 2 and f"does not import `app.modules.{STATIC}`" in found[0]
    assert f"`app.modules.{READONLY}`" in found[1]
    good = {f"app/modules/{ENTRY}.py": f"from app.modules.{STATIC} import install as _static\n"
                                       f"from app.modules.{READONLY} import ReadOnlySession\n"}
    assert es.entry_leaves_out(good, ROW) == []
    # any other step: nothing to check
    assert es.entry_leaves_out(inline, {"node_key": "T8", "tool_config": {"fixes_module": ENTRY}}) == []


# ── the delivery end ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dj(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("deliver_job_2000", REPO / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    (tmp_path / "app" / "modules").mkdir(parents=True)
    (tmp_path / "app" / "labs.py").write_text(f'LABS: tuple[str, ...] = ("{ENTRY}",)\n')
    (tmp_path / "app" / "main.py").write_text("from app.modules import engine_thing\n")
    (tmp_path / "app" / "modules" / "engine_thing.py").write_text("X = 1\n")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    return mod


def _export(sources: dict[str, str]) -> dict:
    steps = {ENTRY: "T8", VIEW: "T6", METRICS: "T5", STATIC: "T9", READONLY: "T10"}
    return {"job_id": "69fe61c4-0000-4000-8000-000000000000", "delivered_at": None, "tables": [], "secrets": [],
            "wiring": [ENTRY, STATIC],
            "files": [{"path": f"app/modules/{m}.py", "step": steps[m], "lines": 1, "content": c}
                      for m, c in sources.items()]}


def test_the_live_delivery_names_each_module_nothing_reaches(dj, capsys):
    assert dj.deliver(_export(SOURCES), ENTRY) == 1
    err = capsys.readouterr().err
    assert f"UNREACHED app/modules/{STATIC}.py (step T9)" in err
    assert f"UNREACHED app/modules/{READONLY}.py (step T10)" in err
    assert f"`{ENTRY}` (the lab's entry" in err and "/complete-plan" in err
    assert VIEW not in err and METRICS not in err


def test_a_delivery_whose_entry_uses_every_module_passes(dj, capsys):
    src = copy.deepcopy(SOURCES)
    src[ENTRY] += f"from app.modules import {STATIC}, {READONLY}\n"
    assert dj.deliver(_export(src), ENTRY) == 0
    assert "UNREACHED" not in capsys.readouterr().err


def test_a_module_the_engine_imports_is_reached_without_a_lab(dj, tmp_path):
    (tmp_path / "app" / "main.py").write_text(f"from app.modules.engine_thing import X\nimport app.modules.{STATIC}\n")
    for m in (STATIC, READONLY):
        (tmp_path / "app" / "modules" / f"{m}.py").write_text("Y = 1\n")
    assert dj.unreached(tmp_path, [STATIC, READONLY]) == [READONLY]
