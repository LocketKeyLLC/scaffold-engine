"""§17.2006 — a change to a view reaches the page it renders: a step that fixes a module is shown the page templates
the module renders and may rewrite them; the sandbox carries the job's pages; a no-key test that stands in for the
page's template does not count for §17.2003; and a fix step that finished with its view still failing gets a
successor.

Live (69fe61c4 NOKEY, 2026-11-23/24): the dashboard learned to mark the SPY benchmark unavailable, but only its
built-in fallback page said so. The step was never shown `app/templates/paper/dashboard.html` (the page the engine
serves), and every no-key test rendered through `_no_template()`, which patches `entry.templates.TemplateResponse` to
raise `TemplateNotFound`. Live, the page drew an empty SPY line with no word why."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.modules import engine_contracts as ec
from app.modules import engine_plan as ep
from app.modules import engine_source as es
from tests.test_a_view_answers_without_a_secret_not_stored_17_2003 import ENGINE, HEAD, TEST_PATH, WIRE, WIRE_PATH

#: the wiring module, rendering a page template
PAGE_WIRE = WIRE.replace('return {"span": span, "spy": await _spy()}',
                         'return templates.TemplateResponse(request, "lab/dashboard.html", {"spy": await _spy()})')
TEMPLATE = '<html><body>{% if spy_unavailable %}<p>unavailable</p>{% endif %}</body></html>\n'
#: NOKEY's shape: the no-key test renders through a helper that patches the template away
NO_TEMPLATE = HEAD.replace("from fastapi import FastAPI", "from contextlib import contextmanager\n\nfrom fastapi import FastAPI") \
    .replace("from app.modules.lab_wire import install", "from app.modules import lab_wire as entry\nfrom app.modules.lab_wire import install") + '''

@contextmanager
def _no_template():
    with patch.object(entry.templates, "TemplateResponse", side_effect=RuntimeError("no template")):
        yield


def test_dashboard_answers_without_the_lab_keys():
    with patch("app.modules.lab_client.values_for", AsyncMock(return_value={})):
        with _no_template():
            assert _client().get("/labs/lab/dashboard").status_code == 200
'''
REAL_TEMPLATE = NO_TEMPLATE.replace("        with _no_template():\n            assert", "        assert")


def _files(test: str) -> dict[str, str]:
    return {WIRE_PATH: PAGE_WIRE, TEST_PATH: test}


# ── the gate ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_no_key_test_that_stands_in_for_the_page_template_is_refused_naming_the_helper():
    found = ec.views_fail_without_secrets(_files(NO_TEMPLATE), ENGINE, {"lab_wire"})
    assert len(found) == 1, found
    assert ("`test_dashboard_answers_without_the_lab_keys` requests it with no secret, but stands in for the page's "
            "template (through `_no_template`)") in found[0]
    assert "render the real template (the sandbox has it)" in found[0]


def test_the_same_test_rendering_the_real_template_passes():
    assert "_no_template()" not in REAL_TEMPLATE.split("def test_")[1]
    assert ec.views_fail_without_secrets(_files(REAL_TEMPLATE), ENGINE, {"lab_wire"}) == []


@pytest.mark.parametrize("stand_in", [
    '    with patch("app.modules.lab_wire.templates.TemplateResponse", side_effect=RuntimeError()):\n',
    '    with patch.object(entry, "templates"):\n',
])
def test_a_stand_in_for_the_template_in_the_test_itself_or_a_fixture_counts(stand_in):
    direct = REAL_TEMPLATE.replace('    with patch("app.modules.lab_client.values_for"', stand_in
                                   + '     with patch("app.modules.lab_client.values_for"')
    direct = direct.replace("\n     with patch(", "\n        with patch(").replace(
        "        assert _client()", "            assert _client()")
    found = ec.views_fail_without_secrets(_files(direct), ENGINE, {"lab_wire"})
    assert len(found) == 1 and "stands in for the page's template (through `test_dashboard_answers" in found[0], found
    fixture = REAL_TEMPLATE.replace("from contextlib import contextmanager", "import pytest\nfrom contextlib import contextmanager") \
        .replace("@contextmanager\ndef _no_template", "@pytest.fixture\ndef no_page") \
        .replace("def test_dashboard_answers_without_the_lab_keys()", "def test_dashboard_answers_without_the_lab_keys(no_page)")
    found = ec.views_fail_without_secrets(_files(fixture), ENGINE, {"lab_wire"})
    assert len(found) == 1 and "(through `no_page`)" in found[0], found


def test_a_view_that_renders_no_page_is_not_held_to_its_template():
    assert ec.views_fail_without_secrets({WIRE_PATH: WIRE, TEST_PATH: NO_TEMPLATE.replace("/labs/lab/dashboard", "/labs/lab/dashboard")},
                                         ENGINE, {"lab_wire"}) == []


# ── the step is shown the page ─────────────────────────────────────────────────────────────────────────────────

def _row(key, out, status="done"):
    return {"node_key": key, "status": status, "output_text": out, "title": key, "prompt_template": ""}


def test_a_fix_step_is_shown_the_template_its_module_renders_the_jobs_copy_first(tmp_path, monkeypatch):
    (tmp_path / "templates" / "lab").mkdir(parents=True)
    (tmp_path / "templates" / "lab" / "dashboard.html").write_text("<html>engine's</html>\n")
    (tmp_path / "templates" / "lab" / "other.html").write_text("<html>not rendered</html>\n")
    (tmp_path / "modules").mkdir()
    monkeypatch.setattr(es, "ENGINE_PKG", tmp_path)
    rows = [_row("T8", f"```python\n# {WIRE_PATH}\n{PAGE_WIRE}```\n")]
    assert es.rendered_templates(PAGE_WIRE, rows) == {"app/templates/lab/dashboard.html": ("<html>engine's</html>\n",
                                                                                          "the engine's own, as merged")}
    rows.append(_row("T7", f"```html\n<!-- app/templates/lab/dashboard.html -->\n{TEMPLATE}```\n"))
    block = es.rendered_templates_block(rows, "lab_wire")
    assert "## The page templates this module renders, as they are now (§17.2006)" in block
    assert "### `app/templates/lab/dashboard.html` (step T7)" in block and "{% if spy_unavailable %}" in block
    assert "<!-- app/templates/lab/dashboard.html -->" in block and "never stand in for the template" in block
    assert "other.html" not in block
    assert es.rendered_templates_block(rows, "lab_tracker") == ""                 # renders no page


# ── the sandbox carries the job's pages ────────────────────────────────────────────────────────────────────────

async def test_the_sandbox_is_given_the_jobs_pages_the_steps_own_copy_winning(monkeypatch):
    from app.sandbox import engine_tests as et
    seen = {}

    async def run_code(files, command, **kw):
        seen.update(files)

        class R:
            ok, error, timed_out, exit_code, stdout, stderr, database = True, None, False, 0, "", "", "ok"
        return R()
    monkeypatch.setattr(et, "run_code", run_code)
    monkeypatch.setattr(et, "_sandbox_engine", AsyncMock(return_value="d"))
    monkeypatch.setattr(et, "_local_digest", lambda: "d")
    siblings = [_row("T7", "```html\n<!-- app/templates/lab/dashboard.html -->\n<html>T7</html>\n```\n"),
                _row("T4", "```json\n// app/static/lab/vendor.json\n{}\n```\n")]
    mine = (f"```python\n# {WIRE_PATH}\n{PAGE_WIRE}```\n\n```python\n# {TEST_PATH}\n{REAL_TEMPLATE}```\n\n"
            "```html\n<!-- app/templates/lab/dashboard.html -->\n<html>mine</html>\n```\n")
    res = await et.check(mine, {"node_key": "NOKEY2"}, siblings)
    assert res.verdict == "pass", res.reason
    assert seen["app/templates/lab/dashboard.html"].strip() == "<html>mine</html>"
    assert seen["app/static/lab/vendor.json"].strip() == "{}"


# ── the plan pass: a fix step that finished with the view still failing gets a successor ───────────────────────

def test_a_finished_fix_step_whose_view_still_fails_gets_a_successor_after_it():
    tasks = [{"id": "T8", "name": "Add dashboard route", "tool": "CodeGen", "status": "done", "depends_on": []},
             {"id": "NOKEY", "name": "Make GET /dashboard answer", "tool": "CodeGen", "status": "done",
              "depends_on": ["T8"], "tool_config": {"fixes_module": "lab_wire", "answers_without_secrets": "lab_wire"}}]
    files = {WIRE_PATH: (PAGE_WIRE, "NOKEY"), TEST_PATH: (NO_TEMPLATE, "NOKEY")}
    assert ep.views_answer_without_secrets(tasks, files, ENGINE) == [
        "views_answer_without_secrets: NOKEY2 edits `lab_wire.py` so `GET /dashboard` answers with no key stored"]
    assert tasks[-1]["depends_on"] == ["NOKEY"] and "what the browser shows is that template" in tasks[-1]["notes"]
    assert ep.views_answer_without_secrets(tasks, files, ENGINE) == []          # NOKEY2 not finished yet
