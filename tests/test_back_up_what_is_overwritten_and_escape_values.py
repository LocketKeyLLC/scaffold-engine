"""§17.1424 — a delivery backs up every path it overwrites; data is never put into an HTML attribute
through markup.

Live, 2026-10-08, ADD126 (the panel's page), read before approval and not approved:
- it overwrote /opt/control-panel-ui/index.html (the Vite app's entry from T34) while the backup covered
  /opt/control-panel-backend only;
- it built `<input value="${value}">` for the Palworld settings, and nine live values hold double quotes
  (`ServerName = "Default Palworld Server"` …): each would have shown empty and Save would have written
  them back empty.
"""
from __future__ import annotations

import inspect
import pathlib
import subprocess

from app.modules import develop as dv
from app.modules import execution_agent
from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures"
PAGE = (FIX / "add126_index_html_2026_10_08.html").read_text()
HOST = dv.Host(guest="111", vm=False, workdir="/opt/control-panel-backend", unit="control-panel.service")
NODE = {"node_key": "ADD126", "title": "page", "description": "x"}


def test_the_live_page_is_refused():
    refs = dv.values_put_into_html_attributes({"/opt/control-panel-ui/index.html": PAGE})
    assert len(refs) == 1 and 'value="${value}"' in refs[0]["why"] and "createElement" in refs[0]["why"]


def test_a_page_that_sets_value_directly_passes():
    safe = ("const input = document.createElement('input'); input.value = value; input.dataset.key = key;\n"
            "grid.innerHTML = ''; label.textContent = key;")
    assert dv.values_put_into_html_attributes({"/opt/control-panel-ui/index.html": safe}) == []


def test_text_interpolated_between_tags_is_not_this_rule():
    """Out of scope here (it is a display, not a value that round-trips through a form)."""
    assert dv.values_put_into_html_attributes({"/x/a.js": "el.innerHTML = `<b>${count}</b>`;"}) == []


def _deliver(files):
    rb = dv.render_delivery(NODE, HOST, files, ["true"])
    return {f["path"]: f["content"] for f in sr.file_writes(rb)}["/tmp/scaffold-dev-ADD126--deliver.sh"]


def test_the_backup_covers_a_file_outside_the_workdir():
    sh = _deliver({"/opt/control-panel-backend/server.js": "x", "/opt/control-panel-ui/index.html": "y"})
    line = next(ln for ln in sh.splitlines() if "tar czf" in ln)
    assert "--ignore-failed-read" in line
    assert line.endswith("-C / opt/control-panel-backend opt/control-panel-ui/index.html")


def test_a_delivery_inside_the_workdir_backs_up_only_the_workdir():
    sh = _deliver({"/opt/control-panel-backend/server.js": "x"})
    assert next(ln for ln in sh.splitlines() if "tar czf" in ln).endswith("-C / opt/control-panel-backend")


def test_bash_level_a_file_not_there_yet_does_not_stop_the_backup(tmp_path):
    root = tmp_path / "root"
    (root / "opt/control-panel-backend").mkdir(parents=True)
    (root / "opt/control-panel-backend/server.js").write_text("x")
    r = subprocess.run(["tar", "czf", str(tmp_path / "b.tgz"), "--ignore-failed-read", "--exclude=node_modules",
                        "-C", str(root), "opt/control-panel-backend", "opt/control-panel-ui/index.html"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    listed = subprocess.run(["tar", "tzf", str(tmp_path / "b.tgz")], capture_output=True, text=True).stdout
    assert "opt/control-panel-backend/server.js" in listed


def test_the_loop_judges_it_every_round():
    assert "develop.values_put_into_html_attributes(files)" in inspect.getsource(execution_agent._pause_for_decision)
