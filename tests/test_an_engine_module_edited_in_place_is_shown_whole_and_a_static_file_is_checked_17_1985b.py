"""§17.1985b — a step that edits an engine module in place is shown that module whole, from the engine's tree when no
step of the job wrote it; and a static file a step for the engine writes is checked mechanically, not by the verifier
alone.

Live (69fe61c4, 2026-11-21): T8 "Add dashboard route" extends `paper_wire_engine_entry_module.py`, the lab's router,
which job 5b4880c0 delivered and was merged -- no row of this job wrote it, so `edited_module_block` showed nothing and
the step would have rewritten the router from its signatures. T4 (the vendor manifest) and T7 (the page template) write
no Python: the engine-test sandbox skips them ("no Python file to run")."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app.modules import engine_plan as ep
from app.modules import engine_source as es

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def _first():
    spec = importlib.util.spec_from_file_location(
        "t17_1985", HERE / "test_a_job_for_the_engine_ships_files_not_shell_on_a_guessed_guest_17_1985.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── (2) an engine module edited in place is shown whole ──────────────────────────────────────────────────────────

def test_the_step_that_extends_the_labs_router_is_planned_as_a_fix_of_that_module(monkeypatch):
    t = _first()
    index = {**es.module_index(), "app.modules.paper_wire_engine_entry_module": {"doc": "", "names": {"install"}}}
    monkeypatch.setattr(ep, "module_index", lambda: index)
    tasks = t._stored_plan()
    ep.complete_engine_plan(tasks, t.BRIEF, [])
    by = {x["id"]: x for x in tasks}
    assert by["T8"]["tool_config"] == {"fixes_module": "paper_wire_engine_entry_module"}
    # §17.2000 — and the step that makes the entry use T9/T10 (planned after it) edits it too; no other step does
    assert by["REACH"]["tool_config"]["fixes_module"] == "paper_wire_engine_entry_module"
    assert not any((x.get("tool_config") or {}).get("fixes_module") for x in tasks if x["id"] not in ("T8", "REACH"))
    assert ep.complete_engine_plan(tasks, t.BRIEF, []) == []                     # a second pass adds nothing


def test_a_module_no_step_wrote_is_shown_whole_from_the_engines_tree():
    path = ROOT / "app/modules/paper_wire_engine_entry_module.py"
    block = es.edited_module_block([], "paper_wire_engine_entry_module")
    assert "## The file this step edits: `app/modules/paper_wire_engine_entry_module.py`" in block
    assert "(step the engine's own, as merged)" in block
    assert path.read_text(encoding="utf-8") in block                              # whole, not its signatures
    assert "keep every public function" in block


def test_a_module_the_job_wrote_wins_and_a_name_that_is_no_module_shows_nothing():
    rows = [{"node_key": "T3", "status": "done", "output_text":
             "```python\n# app/modules/paper_wire_engine_entry_module.py\ndef install(app):\n    return 'job'\n```"}]
    block = es.edited_module_block(rows, "paper_wire_engine_entry_module")
    assert "(step T3)" in block and "return 'job'" in block
    assert es.edited_module_block([], "no_such_module_17_1985b") == ""
    assert es.edited_module_block([], "../config") == ""


def test_the_executor_shows_the_fixed_module_from_tool_config():
    """Production reads `tool_config.fixes_module` and hands it to `edited_module_block` (§17.1832 / §17.1983)."""
    src = (ROOT / "app/modules/execution_agent.py").read_text()
    seg = src[src.index("a step that edits a module in place sees that module as it is now"):][:1200]
    assert "edited_module_block(" in seg and ("fixes_module" in seg or "fixed_module(" in seg)


# ── (3) a static file is checked mechanically ────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><script src="/labs/paper/static/vendor/chart.umd.min.js"></script></head>
<body>
{% for p in proposals %}<details><summary>{{ p.summary }}</summary><p>{{ p.rationale }}</details>{% endfor %}
<button data-span="1w">1w</button><br>
</body></html>
"""
ASKED = {"prompt_template": "A static file this job carries: `app/templates/paper/dashboard.html`, in one block."}


def _html(body: str, path: str = "app/templates/paper/dashboard.html") -> str:
    return f"Here it is.\n\n```html\n<!-- {path} -->\n{body}```\n"


def test_a_page_that_parses_balances_and_loads_its_own_copy_passes():
    assert es.static_file_findings(_html(PAGE), ASKED) == []


@pytest.mark.parametrize("body,why", [
    (PAGE.replace("{% endfor %}", ""), "not a template that parses"),
    (PAGE.replace("{{ p.summary }}", "{{ p.summary }"), "not a template that parses"),
    (PAGE.replace("</details>", ""), "`<details>` is not closed"),
    (PAGE.replace("/labs/paper/static/vendor", "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist"),
     "loads from another origin"),
    (PAGE.replace('<meta charset="utf-8">', '<link rel="stylesheet" href="//fonts.example-cdn.net/a.css">'),
     "loads from another origin"),
])
def test_a_page_that_does_not_parse_balance_or_stay_on_its_origin_is_refused(body, why):
    found = es.static_file_findings(_html(body), ASKED)
    assert any(why in f for f in found), found


def test_the_planned_file_missing_from_the_output_is_refused():
    found = es.static_file_findings("```html\n" + PAGE + "```\n", ASKED)
    assert found and found[0].startswith("`app/templates/paper/dashboard.html`: the step writes this file")


MANIFEST = "```json\n// app/static/paper/vendor.json\n{body}\n```\n"


def test_a_vendor_manifest_parses_and_names_pinned_https_files():
    good = {"chart.umd.min.js": "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"}
    assert es.static_file_findings(MANIFEST.format(body=json.dumps(good)), {}) == []
    for bad, why in (({"chart.umd.min.js": "https://cdn.jsdelivr.net/npm/chart.js@latest/dist/chart.umd.min.js"},
                      "pinned release"),
                     ({"chart.umd.min.js": "http://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"},
                      "pinned release"),
                     ({"../chart.js": "https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"},
                      "not a file name"),
                     ([], "object of file name")):
        found = es.static_file_findings(MANIFEST.format(body=json.dumps(bad)), {})
        assert any(why in f for f in found), (bad, found)
    assert any("not JSON that parses" in f for f in es.static_file_findings(MANIFEST.format(body="{'a': 1,}"), {}))


@pytest.mark.parametrize("lang,path,body,why", [
    ("css", "app/static/paper/dashboard.css", "@import url('https://fonts.example-cdn.net/x.css');\nb{}\n",
     "another origin"),
    ("js", "app/static/paper/dashboard.js", "import Chart from 'https://cdn.jsdelivr.net/npm/chart.js@4.5.1/+esm';\n",
     "another origin"),
    ("js", "app/static/paper/dashboard.js", "   \n", "is empty"),
])
def test_a_stylesheet_or_script_is_not_empty_and_loads_nothing_from_another_origin(lang, path, body, why):
    header = f"/* {path} */" if lang == "css" else f"// {path}"
    found = es.static_file_findings(f"```{lang}\n{header}\n{body}```\n", {})
    assert any(why in f for f in found), found
    assert es.static_file_findings(f"```{lang}\n{header}\nconst span = '1w';\n```\n", {}) == []


def test_a_python_output_has_no_static_findings():
    assert es.static_file_findings("```python\n# app/modules/x.py\nX = 1\n```", {"prompt_template": "Python module"}) == []


def test_the_manifest_rules_are_the_ones_the_delivery_applies():
    spec = importlib.util.spec_from_file_location("deliver_job_17_1985b", ROOT / "scripts/deliver_job.py")
    dj = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dj
    spec.loader.exec_module(dj)
    assert dj._PINNED.pattern == es.PINNED_RE.pattern and dj._VENDOR_NAME.pattern == es.VENDOR_NAME_RE.pattern


@pytest.mark.integration
async def test_engine_findings_refuses_a_broken_page_for_an_engine_job():
    """The wiring: the gate the executor runs on every CodeGen output for the engine carries the static checks."""
    import uuid

    from app.database import async_session
    from app.modules import node_editor
    text = node_editor.text
    jid = str(uuid.uuid4())
    brief = {"title": "dash", "description": "Deliver the dashboard into the engine through the lab's delivery flow."}
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'dash', 'running', "
                              "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(brief)})
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, prompt_template, tool, execution_order) "
            "VALUES (:j, 'T7', 'Write dashboard HTML template', 'running', :p, 'CodeGen', 0)"),
            {"j": jid, "p": ASKED["prompt_template"]})
        await db.commit()
    try:
        async with async_session() as db:
            bad = await es.engine_findings(db, jid, "T7", _html(PAGE.replace("{% endfor %}", "")))
            good = await es.engine_findings(db, jid, "T7", _html(PAGE))
        assert any("not a template that parses" in f for f in bad), bad
        assert not any("app/templates/paper/dashboard.html" in f for f in good), good
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
