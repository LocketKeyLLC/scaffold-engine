"""§17.1419 — the settings round trip is the test for a step that SAVES; every other developed step is
tested by its own requests; and the engine's own address is a value it holds.

Live, 2026-10-08, ADD124 (a read-only proxy to the engine's /health), eight rounds refused:
- the rehearsal ran the GET-then-PUT round trip on it because its text names a route and a measured
  service -- and the model, to pass, invented `PUT /api/scaffold-engine` forwarding a PUT to /health;
- the address gate called `http://192.168.1.43:8000` "a guess" -- the URL the engine itself had put
  in the prompt (§17.1418).
"""
from __future__ import annotations

import inspect
import json
import pathlib

from app.modules import develop as dv
from app.modules import execution_agent
from app.modules import rehearsal as rh
from app.modules import service_truth as st
from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD122 = json.loads((FIX / "add122_node_2026_10_07.json").read_text())
ADD123 = json.loads((FIX / "add123_node_2026_10_07.json").read_text())
ADD124 = json.loads((FIX / "add124_node_2026_10_08.json").read_text())
PANEL = st.ServiceTruth(guest="111", name="control-panel", unit="control-panel.service",
                        workdir="/opt/control-panel-backend", configs=("/opt/control-panel-backend/config/capabilities.json",))
PAL = st.ServiceTruth(guest="106", vm=True, name="palworld",
                      configs=("/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
                               "/opt/palworld/DefaultPalWorldSettings.ini"))


def test_a_read_only_proxy_gets_no_round_trip():
    assert rh.roundtrip_target(ADD124, [PANEL, PAL]) is None


def test_the_settings_editor_still_does():
    assert rh.roundtrip_target(ADD122, [PANEL, PAL]) is not None


def test_its_own_read_check_becomes_the_sandbox_request():
    assert rh.acceptance_requests(dv.own_checks(ADD124)) == [
        {"method": "GET", "url": "http://127.0.0.1:3001/api/scaffold-engine", "body": None}]


def test_a_write_check_still_does_too():
    assert [r["method"] for r in rh.acceptance_requests(dv.own_checks(ADD123))] == ["POST"]


def test_the_loop_uses_every_own_check():
    assert "rehearsal.acceptance_requests(develop.own_checks(run_node))" in inspect.getsource(execution_agent._pause_for_decision)


ENV = {"facts": [{"text": "CT 111 control-panel at 192.168.1.25"}]}
CFG = [{"path": "/tmp/scaffold-dev-ADD124--opt--control-panel-backend--config--scaffold-engine.json",
        "content": '{"engineUrl": "http://192.168.1.43:8000"}'}]


def test_the_engines_own_address_is_held():
    assert sr.unsourced_addresses_in_files([], CFG, ENV, ADD124, "", ["192.168.1.43"]) == []


def test_any_other_address_is_still_a_guess():
    """Vacuity guard: the gate still bites on an address nothing holds."""
    other = [{**CFG[0], "content": '{"engineUrl": "http://192.168.1.77:8000"}'}]
    assert sr.unsourced_addresses_in_files([], other, ENV, ADD124, "", ["192.168.1.43"])
    assert sr.unsourced_addresses_in_files([], CFG, ENV, ADD124, "", [None, ""])      # not held -> refused


def test_frame_run_passes_the_engines_address():
    src = inspect.getsource(sr.frame_run)
    assert '[engine_address, os.environ.get("SCAFFOLD_LAN_ADDRESS")]' in src
