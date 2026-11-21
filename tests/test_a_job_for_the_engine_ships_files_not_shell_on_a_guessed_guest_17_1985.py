"""§17.1985 — a job whose deliverable is code for the engine itself plans its file work as the job's own files and its
delivery as the delivery question, never as Shell steps on a homelab runner against a guessed guest.

Live (69fe61c4 "Paper-Trading Lab Operator Dashboard", 2026-11-21): the brief said "Deliver the dashboard into the
engine through the lab's existing delivery flow"; no pattern read it as code for the engine, so T4 "Vendor charting
asset", T9, T10 and T12 "Deliver dashboard via lab flow" were Shell, and T4 was framed as
`pct exec <ENGINE_GUEST_ID> -- sh -c 'curl … -o <LAB_STATIC_DIR>/vendor/chart.umd.min.js'` on the Proxmox host."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.modules import engine_plan as ep
from app.modules import engine_source as es

ROOT = Path(__file__).resolve().parent.parent

# the job's stored brief (its goals, description, inputs and outputs, as refined) and the operator's idea
BRIEF = {
    "title": "Paper-Trading Lab Operator Dashboard",
    "description": ("Add a read-only operator dashboard to the paper-trading lab already living in the engine: an HTML "
                    "page served under the lab's existing /labs/paper router at /labs/paper/dashboard, gated by the "
                    "same API-key auth as the lab's other routes. ... the page ships through the same delivery flow "
                    "that delivered the lab itself (job 5b4880c0)."),
    "goals": ["Add an HTML dashboard route at /labs/paper/dashboard inside the existing /labs/paper router in "
              "app/modules/paper_wire_engine_entry_module.py, served by the engine and gated by the same API-key auth "
              "as the lab's other routes.",
              "Add no live trading logic and no new secrets.",
              "Deliver the dashboard into the engine through the lab's existing delivery flow, as job 5b4880c0 did "
              "for the lab."],
    "constraints": ["Use the existing engine delivery flow rather than hand-copied files or a separate deployment "
                    "path."],
    "inputs_available": ["Existing lab modules in the engine at app/modules/paper_*.py",
                         "Existing lab routes in app/modules/paper_wire_engine_entry_module.py under the /labs/paper "
                         "prefix"],
    "outputs_expected": ["An HTML dashboard page served at /labs/paper/dashboard",
                         "The dashboard delivered into the engine via the lab's delivery flow"],
}
IDEA = ("Build the paper-trading lab's operator dashboard ... Deliver it into the engine like the lab itself (the lab's "
        "delivery flow). The lab is already in the engine: app/modules/paper_*.py, routes in "
        "app/modules/paper_wire_engine_entry_module.py (prefix /labs/paper), delivered by job 5b4880c0.")


def _stored_plan() -> list[dict]:
    """The job's stored plan, step for step (titles, tools, dependencies and the planner's own text)."""
    s = lambda i, n, tool, deps, notes, typ="action": {"id": i, "name": n, "tool": tool, "type": typ,  # noqa: E731
                                                       "depends_on": deps, "inputs": [], "outputs": [], "notes": notes}
    return [
        s("T1", "Choose charting asset delivery", "LLM", [], "Operator decision. Options: (a) Vendor Chart.js 4.5.1 "
          "UMD locally ...", "decision"),
        s("T2", "Inspect existing lab modules", "Milvus", [], "Read-only survey of the existing lab code. No edits."),
        s("T3", "Pin metric definitions", "LLM", ["T2"], "Text decision only.", "decision"),
        s("T4", "Vendor charting asset", "Shell", ["T1"], "Starts from T1. If vendoring: place the pinned Chart.js "
          "4.5.1 UMD file under the lab's static asset path and record its SHA-256. Only the asset acquisition -- no "
          "route or template work."),
        s("T5", "Write metrics computation module", "CodeGen", ["T3"], "Pure read-only computation from existing "
          "data. Exports compute_series(view, spy, span) and span-filter helper."),
        s("T6", "Write dashboard view builder", "CodeGen", ["T2", "T5"], "Reshapes existing view/audit/proposal data "
          "into template context. Imports compute_series from T5."),
        s("T7", "Write dashboard HTML template", "CodeGen", ["T4", "T6"], "Template renders context from T6 and "
          "references the asset from T4. Span buttons 1w/1m/3m/all. No route logic."),
        s("T8", "Add dashboard route", "CodeGen", ["T6", "T7"], "Thin route in paper_wire_engine_entry_module.py: "
          "imports build_context (T6) and renders dashboard.html (T7), reuses the existing API-key auth dependency. "
          "Does NOT alter existing routes' behaviour/auth."),
        s("T9", "Serve static asset under router", "Shell", ["T4", "T8"], "Wires the lab's static serving so the "
          "pinned bundle is reachable under the router. Only static serving -- no route or template edits."),
        s("T10", "Enforce read-only data access", "Shell", ["T5", "T6", "T8"], "Structurally enforce read-only "
          "(read-only transaction) on the dashboard's query path. No schema changes."),
        s("T11", "Verify existing routes unchanged", "LLM", ["T8"], "Existing /labs/paper routes still behave. No "
          "edits.", "checkpoint"),
        s("T12", "Deliver dashboard via lab flow", "Shell", ["T8", "T9", "T10"], "Ship the new template/static/module "
          "files through the existing delivery flow that delivered the lab (job 5b4880c0). No hand-copied files, no "
          "separate deployment path."),
        s("T13", "Verify dashboard renders", "LLM", ["T12"], "Checks the page loads, chart draws. No edits.",
          "checkpoint"),
        s("T16", "Document the dashboard", "LLM", ["T13"], "Documentation about the delivered dashboard -- text "
          "only."),
    ]


# ── recognition ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_dashboard_job_is_code_for_the_engine_from_its_brief_and_from_the_operators_words():
    assert es.targets_the_engine(BRIEF)
    assert es.idea_targets_the_engine(IDEA)
    assert es.targets_the_engine(es.brief_for_the_engine({"title": "Operator dashboard"}, IDEA))


@pytest.mark.parametrize("text", [
    # the engine is placed on a machine; none of its code is written (the server move, 171 Shell steps)
    "Situate the scaffold engine itself on the Proxmox server (hv-one, 10.20.30.40), so the engine runs there",
    "Move the scaffold engine itself onto the new server and keep the laptop as a client",
    "Deploy bitmagnet on Proxmox behind AirVPN and add it to Prowlarr as an indexer",
    "Give any login the engine does not hold; the engine reads the hardware itself",
    "Deliver the media files into the library folder the server reads",
])
def test_a_homelab_job_is_not_code_for_the_engine(text):
    assert not es.idea_targets_the_engine(text)
    assert not es.targets_the_engine({"title": "Homelab work", "description": text})


def test_the_earlier_lab_briefs_still_target_the_engine():
    assert es.idea_targets_the_engine("Finish the paper-trading research lab in the scaffold engine itself (this "
                                      "repository's code).")
    assert es.targets_the_engine({"description": "Add a research lab to the scaffold engine itself"})


# ── the plan ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _the_labs_router_is_the_engines(monkeypatch):
    """The lab 5b4880c0 delivered is in the engine (conftest hides delivered labs from the engine's own tests): its
    router is an engine module, which this job extends."""
    index = {**es.module_index(), "app.modules.paper_wire_engine_entry_module": {"doc": "", "names": {"install"}}}
    monkeypatch.setattr(ep, "module_index", lambda: index)


def test_the_stored_plan_completed_has_no_shell_step_and_delivers_through_the_question():
    tasks = _stored_plan()
    changes = ep.complete_engine_plan(tasks, BRIEF, [])
    by = {t["id"]: t for t in tasks}
    assert not [t["id"] for t in tasks if str(t.get("tool")).lower() == "shell"], changes
    # the delivery is the engine's delivery question, after every code step
    d = by["T12"]
    assert d["name"] == "Is this job's code merged into the engine and running?" and d["type"] == "decision"
    assert d["tool"] == "LLM" and d["notes"] == ep._DELIVER_NOTE
    code = {t["id"] for t in tasks if t["tool"] == "CodeGen"}
    assert {"T4", "T5", "T6", "T7", "T8", "T9", "T10"} <= code <= set(d["depends_on"])
    assert "T12" in by["T13"]["depends_on"]
    # the vendored library is named in the lab's vendor manifest, never fetched on a host
    assert by["T4"]["notes"].startswith(ep._STATIC_LEAD + ": `app/static/paper/vendor.json`")
    assert "pct" not in by["T4"]["notes"] and "`scripts/deliver_job`" in by["T4"]["notes"]
    assert es.step_module({"prompt_template": by["T4"]["notes"], "title": by["T4"]["name"]}) == ""
    assert es.step_module({"prompt_template": by["T7"]["notes"], "title": by["T7"]["name"]}) == ""
    assert "tests/test_" not in by["T4"]["notes"] and "tests/test_" not in by["T7"]["notes"]   # no module, no tests
    # the template is a static file the export carries, not a Python module
    assert by["T7"]["notes"].startswith(ep._STATIC_LEAD + ": `app/templates/paper/dashboard.html`")
    assert ".py" not in by["T7"]["notes"].split(" -- ")[0]
    # the route extends the lab's own router in place; it is not renamed to a new module
    assert by["T8"]["notes"].startswith("Python module `paper_wire_engine_entry_module.py` -- the engine's own")
    assert not any("paper_paper_wire" in str(t.get("notes")) for t in tasks)
    # the read-only guard and static serving are code the job writes
    assert any(c.startswith("shell_made_code: T10") for c in changes)
    assert any(c.startswith("delivery_made_the_question: T12") for c in changes)
    # documentation and checkpoints are left alone
    assert by["T16"]["tool"] == "LLM" and by["T13"]["tool"] == "LLM"


def test_a_second_pass_changes_nothing_and_a_second_delivery_step_is_dropped():
    tasks = _stored_plan()
    tasks.append({"id": "T17", "name": "Merge the dashboard into the engine", "tool": "Shell", "type": "action",
                  "depends_on": ["T12"], "inputs": [], "outputs": [], "notes": "Merge the branch into the engine."})
    ep.complete_engine_plan(tasks, BRIEF, [])
    assert "T17" not in {t["id"] for t in tasks}
    assert sum(ep._DELIVER_QUESTION_RE.match(t["name"]) is not None for t in tasks) == 1
    snapshot = json.dumps(tasks, sort_keys=True, default=str)
    assert ep.complete_engine_plan(tasks, BRIEF, []) == []
    assert json.dumps(tasks, sort_keys=True, default=str) == snapshot


@pytest.mark.parametrize("name", ["Wire engine entry module", "Write the delivery notes for the operator",
                                  "Document the delivered dashboard"])
def test_a_step_that_does_not_ship_the_code_is_not_the_delivery_question(name):
    assert not ep._DELIVERY_STEP_RE.search(name)


@pytest.mark.parametrize("name,hit", [("Vendor charting asset", True), ("Download the Chart.js bundle", True),
                                      ("Fetch chart.umd.min.js", True), ("Fetch SPY benchmark bars", False),
                                      ("Download the SEC filings index", False)])
def test_a_vendor_step_is_a_third_party_file_not_the_labs_data(name, hit):
    assert bool(ep._VENDOR_STEP_RE.match(name)) is hit


def test_a_homelab_plan_is_never_completed_as_engine_code():
    """The planner completes a plan as engine code only for a brief that targets the engine; a homelab job's Shell
    steps on its runner stay Shell (no other pass reads `_DELIVERY_STEP_RE`)."""
    brief = {"title": "Deploy bitmagnet on Proxmox behind AirVPN",
             "description": "Create an LXC on the Proxmox host, install bitmagnet, route it through the VPN.",
             "goals": ["Deliver the container's address to Prowlarr"]}
    assert not es.targets_the_engine(brief)
    src = (ROOT / "app/modules/dag_generator.py").read_text()
    assert "if engine_source.targets_the_engine(_brief_d):" in src


def test_extended_modules_reads_engine_paths_the_brief_names():
    assert ep.extended_modules(BRIEF) == {"paper_wire_engine_entry_module"}
    assert ep.extended_modules({"description": "a homelab job"}) == set()


# ── the export carries the static files ──────────────────────────────────────────────────────────────────────────

OUTPUT = """Here is the page.

```html
<!-- app/templates/paper/dashboard.html -->
<!doctype html><title>Paper lab</title><script src="/labs/paper/static/vendor/chart.umd.min.js"></script>
```

```json
// app/static/paper/vendor.json
{"chart.umd.min.js": "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"}
```

```js
// app/static/../../etc/evil.js
alert(1)
```

```python
# app/modules/paper_dashboard_view.py
def build_context():
    return {}
```
"""


def test_static_files_are_exported_beside_the_code_and_a_path_outside_the_lab_folders_is_not():
    rows = [{"node_key": "T7", "status": "done", "output_text": OUTPUT, "title": "Write dashboard HTML template"},
            {"node_key": "T9", "status": "pending", "output_text": "```css\n/* app/static/paper/x.css */\nb{}\n```"}]
    static = es.job_static_files(rows)
    assert set(static) == {"app/templates/paper/dashboard.html", "app/static/paper/vendor.json"}
    assert static["app/templates/paper/dashboard.html"][0].startswith("<!doctype html>")
    assert json.loads(static["app/static/paper/vendor.json"][0])["chart.umd.min.js"].endswith("chart.umd.min.js")
    assert set(es.job_files(rows)) == {"app/modules/paper_dashboard_view.py"}       # the Python export is unchanged


def _deliver_job():
    spec = importlib.util.spec_from_file_location("deliver_job_17_1985", ROOT / "scripts/deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_deliver_job_writes_static_files_and_fetches_only_pinned_vendor_files():
    dj = _deliver_job()
    assert dj._SAFE.match("app/templates/paper/dashboard.html") and dj._SAFE.match("app/static/paper/vendor.json")
    assert not dj._SAFE.match("app/static/../x.js") and not dj._SAFE.match("app/main.py")
    assert dj.vendored("app/static/paper/vendor.json", json.dumps(
        {"chart.umd.min.js": "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"})) == [
        ("app/static/paper/vendor/chart.umd.min.js", "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js")]
    assert dj.vendored("app/templates/paper/dashboard.html", "<p>") == []
    for bad in ({"chart.js": "https://cdn.jsdelivr.net/npm/chart.js@latest/dist/chart.umd.min.js"},
                {"chart.js": "http://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"},
                {"../evil.js": "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"}):
        with pytest.raises(ValueError):
            dj.vendored("app/static/paper/vendor.json", json.dumps(bad))
