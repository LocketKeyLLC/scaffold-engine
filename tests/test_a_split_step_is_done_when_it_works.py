"""§17.1408 — a split step is done when it WORKS, and the engine's own note is said once.

Live, 2026-10-06/07. ADD100 ("rebuild the control panel") was split into 8 steps,
and every capability step got a presence check as its done-condition:
`pct exec 111 -- grep -n 'palworld' /opt/control-panel-backend/server.js`. ADD122
was recorded done on that twice -- once after a run, once by already-met reading the
broken run's leftovers -- while its GET answered `{"settings":{}}` and its PUT could
not write. The operator chose behaviour checks.

And ADD126's description carried the engine's count correction TWELVE times: the
note says "this step says three capabilities", and `count_edits` read that back as
the step's own claim on every pass.
"""
from __future__ import annotations

import pytest

from app.modules import step_decomposition as sd
from app.modules.step_decomposition import builds_a_capability, children_from, count_edits, presence_only

#: the seven checks the live split wrote
LIVE_CHECKS = [
    "pct exec 111 -- grep -n 'palworld' /opt/control-panel-backend/server.js",
    "pct exec 111 -- grep -n 'radarr\\|sonarr' /opt/control-panel-backend/server.js",
    "pct exec 111 -- grep -n 'scaffold' /opt/control-panel-backend/server.js",
    "pct exec 111 -- grep -n 'pihole' /opt/control-panel-backend/server.js",
    "pct exec 111 -- cat /opt/control-panel-ui/index.html",
    "pct exec 111 -- systemctl is-active control-panel.service",
]


@pytest.mark.parametrize("check", LIVE_CHECKS)
def test_every_live_check_is_presence_only(check):
    assert presence_only(check)


@pytest.mark.parametrize("check", [
    "pct exec 111 -- curl -s http://127.0.0.1:3001/api/palworld/settings",
    "curl -s -o /dev/null -w '%{http_code}' https://defrusciohomelab.duckdns.org/",
    "pct exec 111 -- sh -c 'curl -fsS http://127.0.0.1:3001/health'",
])
def test_a_check_that_calls_something_is_not(check):
    assert not presence_only(check)


def test_the_live_capability_step_is_written_as_done_when_it_works():
    steps = [{"title": "LXC 111: implement the Palworld settings capability (read/write the server's own settings file)",
              "description": "On LXC 111, add a backend route that reads and writes the Palworld server's settings.",
              "check": LIVE_CHECKS[0]}]
    out = children_from(steps, parent_key="ADD100", parent_deps=[], keys=["ADD122"])
    d = out[0]["description"]
    assert "Done when it WORKS" in d
    assert "only shows the code is there, which is not done" in d
    assert "` shows it." not in d


def test_a_step_that_builds_no_capability_keeps_its_check():
    steps = [{"title": "Install nodejs on LXC 111", "description": "apt install nodejs",
              "check": "pct exec 111 -- sh -c 'node --version'"}]
    out = children_from(steps, parent_key="P", parent_deps=[], keys=["ADD1"])
    assert "Done when `pct exec 111 -- sh -c 'node --version'` shows it." in out[0]["description"]


@pytest.mark.parametrize("text,yes", [
    ("implement the media request capability", True), ("add a backend route", True),
    ("build the single-page frontend", True), ("add authentication", True),
    ("install nodejs", False), ("set onboot on VM 110", False),
])
def test_what_builds_a_capability(text, yes):
    assert builds_a_capability(text) is yes


def test_the_prompt_asks_for_a_behaviour_check():
    assert "WORKS" in sd.SPLIT_SYSTEM and "only shows the code exists" in sd.SPLIT_SYSTEM
    check_desc = sd.SPLIT_TOOL.input_schema["properties"]["steps"]["items"]["properties"]["check"]["description"]
    assert "WORKS" in check_desc


# ── the note said once ───────────────────────────────────────────────────────

MARK = "[Engine split of ADD100 — {i} of 8]"
NOTE = ("ENGINE MEASURED: the plan holds 4 capability steps (ADD122 Palworld settings, ADD123 media request, "
        "ADD124 scaffold-engine, ADD125 network-view); this step says three capabilities. Cover all four.")


def _plan(add126_desc):
    caps = [("ADD122", "implement the Palworld settings capability"), ("ADD123", "implement the media request capability"),
            ("ADD124", "implement the scaffold-engine capability"), ("ADD125", "implement the network-view capability")]
    plan = [{"node_key": k, "title": f"LXC 111: {t}", "description": "x " + MARK.format(i=i + 2), "status": "pending"}
            for i, (k, t) in enumerate(caps)]
    plan.append({"node_key": "ADD126", "title": "LXC 111: build the single-page frontend rendering the three capabilities",
                 "description": add126_desc, "status": "pending"})
    return plan


def test_the_note_is_not_read_back_as_the_steps_claim():
    """ADD126's TITLE says three: one correction. Its own note must not make a second."""
    desc = "renders four sections. " + MARK.format(i=6) + "\n\n" + NOTE
    out = count_edits(_plan(desc), "ADD100")
    assert not [e for e in out if e.get("description") and e["description"].count(NOTE) > 1]


def test_a_note_already_present_is_not_appended_again():
    desc = "x " + MARK.format(i=6) + "\n\n" + NOTE
    for e in count_edits(_plan(desc), "ADD100"):
        assert "description" not in e or e["description"].count(NOTE) == 1


def test_the_live_twelve_copies_never_grow():
    desc = "x " + MARK.format(i=6) + ("\n\n" + NOTE) * 12
    for e in count_edits(_plan(desc), "ADD100"):
        assert "description" not in e, "a step already carrying the note gets nothing more"
