"""§17.1409 — a draft that edits a service's settings is REHEARSED before it is offered.

Live, 2026-10-06/07. A dozen ADD122 drafts passed every shape gate and would each have
broken the Palworld server's settings or done nothing. Reading could not keep up; the
operator chose "coder model + round-trip test". The coder model was measured and is
not better (0/3 round trips, and no output at all with thinking on). The rehearsal is
the lever: it ran nine real drafts against copies of the real files and failed all
nine for concrete reasons -- and passed a correct route (122 -> 122 settings, header
kept), so it says yes to good work too.

These tests use the sandbox's real reports from that day as fixtures.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import rehearsal as rh
from app.modules import service_truth as st
from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures" / "rehearsal"
CORRUPT = json.loads((FIX / "report_corrupts_the_file.json").read_text())
WRONG_UNIT = json.loads((FIX / "report_wrong_unit.json").read_text())
GOOD = json.loads((FIX / "report_good_route.json").read_text())

LIVE = "/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
TPL = "/opt/palworld/DefaultPalWorldSettings.ini"
PAL = st.ServiceTruth(guest="106", vm=True, name="palworld", configs=(LIVE, TPL), empty_beside=TPL)
PANEL = st.ServiceTruth(guest="111", name="control-panel", unit="control-panel.service",
                        ports=("3001",), workdir="/opt/control-panel-backend")
ADD122 = {"title": "LXC 111: implement the Palworld settings capability (read/write the server's own settings file)",
          "description": "Done when the capability WORKS: `pct exec 111 -- curl -s http://127.0.0.1:3001/api/palworld/settings` "
                         "returns the server's real settings including `ServerName` …"}


# ── what is rehearsed ────────────────────────────────────────────────────────

def test_the_live_step_has_a_round_trip_target():
    t = rh.roundtrip_target(ADD122, [PANEL, PAL])
    assert t == {"get": "http://127.0.0.1:3001/api/palworld/settings", "put": "http://127.0.0.1:3001/api/palworld/settings",
                 "config": LIVE, "baseline": TPL, "config_guest": "106", "config_vm": True, "port": "3001"}


@pytest.mark.parametrize("node", [
    {"title": "Install nodejs on LXC 111", "description": "apt install nodejs"},                     # no route
    {"title": "LXC 111: expose a health route", "description": "curl http://127.0.0.1:3001/health"},  # no service config named
])
def test_a_step_without_both_is_not_rehearsed(node):
    assert rh.roundtrip_target(node, [PANEL, PAL]) is None


# ── what a report means ──────────────────────────────────────────────────────

def test_the_good_route_passes():
    assert GOOD["roundtrip"]["ok"] is True
    assert rh.refusal_from(GOOD) == []


def test_the_corrupting_draft_is_refused_with_the_evidence():
    out = rh.refusal_from(CORRUPT)
    assert len(out) == 1
    why = out[0]["why"]
    assert rh.MARK in why
    assert "lost its `[section]` header" in why
    assert "122 settings before, 1 after" in why
    assert "(Difficulty" in why                      # the key that was never there
    assert "OptionSettings=((Difficulty" in why       # the file after, verbatim


def test_the_wrong_unit_draft_is_refused_naming_the_failing_command():
    why = rh.refusal_from(WRONG_UNIT)[0]["why"]
    assert "Unit control-panel-backend not found" in why
    assert "restart the unit the facts name" in why


@pytest.mark.parametrize("report", [None, {"error": "rehearsal exceeded 240s"}, {"error": "could not copy"}])
def test_a_rehearsal_that_could_not_run_refuses_nothing(report):
    assert rh.refusal_from(report) == []


@pytest.mark.asyncio
async def test_an_unreachable_service_is_none_not_a_refusal():
    out = await rh.rehearse(["true"], [], {"get": "x", "put": "x", "config": "c", "baseline": ""}, [],
                            url="http://127.0.0.1:9")
    assert out is None


# ── seeds come off the machines, read-only ───────────────────────────────────

@pytest.mark.asyncio
async def test_the_seeds_are_the_config_the_baseline_and_the_route_hosts_code(monkeypatch):
    sent: list = []

    async def probe(spec, c):
        sent.append(c)
        if " find /opt/control-panel-backend" in c:
            return True, ("/opt/control-panel-backend/server.js\n/opt/control-panel-backend/server.js.bak.1\n"
                          "/opt/control-panel-backend/node_modules/x/index.js\n/opt/control-panel-backend/config/capabilities.json\n")
        return True, "content of " + c.rsplit(" ", 1)[-1]
    monkeypatch.setattr(st, "_probe", probe)
    t = rh.roundtrip_target(ADD122, [PANEL, PAL])
    seeds = await rh.seeds_for(object(), t, [PANEL, PAL])
    paths = [s["path"] for s in seeds]
    assert paths == [LIVE, TPL, "/opt/control-panel-backend/server.js",
                     "/opt/control-panel-backend/config/capabilities.json", "/etc/systemd/system/control-panel.service"]
    assert any(c.startswith("qm guest exec 106 -- cat ") for c in sent), "the VM's files through its agent"
    assert any(c.startswith("pct exec 111 -- cat ") for c in sent)
    assert not any(" rm " in c or ">" in c for c in sent), "reads only"


# ── it drives a redraft ──────────────────────────────────────────────────────

def test_the_refusal_is_registered_and_the_drafters_gap():
    refused = rh.refusal_from(CORRUPT)
    assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP[rh.MARK] == "drafter"


def test_it_runs_where_every_draft_is_judged():
    from app.modules import execution_agent
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "rehearsal.refusal_from(" in src and "rehearsal.roundtrip_target(" in src
