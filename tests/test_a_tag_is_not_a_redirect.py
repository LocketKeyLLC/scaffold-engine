"""§17.1425 — on a line of HTML markup, a `>` closes a tag; it is not a shell redirect.

Live, 2026-10-08, ADD126 (the panel's page), all eight rounds refused by §17.1405 ("a JSON-encoded value is
spliced into a shell command") for `contentEl.innerHTML = `<div class="result success">${JSON.stringify(data,
null, 2)}</div>`` -- an answer shown on a web page; no shell anywhere. `_SHELLISH_RE`'s redirect branch
`>\\s*\\S` matched the tag's `>`.
"""
from __future__ import annotations

import pathlib

from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures"
PAGE = (FIX / "add126_index_html_v2_2026_10_08.html").read_text()


def test_the_live_page_is_not_refused():
    assert sr.json_used_as_shell_quoting([], [{"path": "/opt/control-panel-backend/public/index.html", "content": PAGE}]) == []


def test_the_live_case_that_made_the_rule_is_still_refused():
    """ADD122, 2026-10-06: `printf '%s' ${JSON.stringify(content)} > …` inside an execSync ssh command."""
    js = "execSync(`ssh root@192.168.1.106 \"printf '%s' ${JSON.stringify(content)} > /opt/palworld/x.ini\"`);"
    assert sr.json_used_as_shell_quoting([], [{"path": "/opt/x/route.js", "content": js}])


def test_a_bare_redirect_with_a_stringified_name_is_still_refused():
    js = "const body = JSON.stringify(s);\nconst cmd = `cat ${body} > /tmp/out.json`;"
    assert sr.json_used_as_shell_quoting([], [{"path": "/opt/x/route.js", "content": js}])
