"""§17.1333 — the engine's own address is measured, not asked of the operator.

ADD124, written by the engine's own split of ADD100, said: "with the engine's
address read from config (operator-supplied value)". It is not an operator's to
supply. The engine cannot see the address from inside its container -- its
interfaces are the docker bridge -- so it asks the machine it drives where it
sees the engine coming from. Live, 2026-10-03, `ss -tn` on the Proxmox host
(192.168.1.156) answered with the engine as the peer on the runner's port 8790;
the fixture below is that output.
"""
from __future__ import annotations

import pathlib

from app.modules import machine_truth as mt
from app.modules import machine_values as mv
from app.modules import supervised_runs as sr

FX = pathlib.Path(__file__).parent / "fixtures"
LIVE_SS = (FX / "pve_ss_tn_2026_10_03.txt").read_text(encoding="utf-8")
RUNNER_ENDPOINT = "http://192.168.1.156:8790/mcp/"
POLICY = {"allow": ["ANY"], "sudo": True, "secrets": ["MASS_PASSWORD"], "can_write_files": True}
NODE = {"node_key": "ADD124", "title": "LXC 111: implement the scaffold-engine capability",
        "description": "On LXC 111, add a backend route that proxies to scaffold-engine's own surface so "
                       "the operator can test larger local models on the Tesla P40."}


def test_the_runners_port_names_the_engine():
    assert mt.port_of(RUNNER_ENDPOINT) == "8790"
    assert mt.peers_of_port(LIVE_SS, "8790") == ["192.168.1.43"]


def test_a_port_two_machines_hold_decides_nothing():
    """Port 22 on that host has two peers: no single answer, so no answer."""
    assert sorted(mt.peers_of_port(LIVE_SS, "22")) == ["192.168.1.10", "192.168.1.43"]


def test_loopback_and_the_v4_mapped_form():
    """The web console's connection is v4-mapped and its loopback pair is skipped."""
    assert mt.peers_of_port(LIVE_SS, "8006") == ["192.168.1.43"]
    assert mt.peers_of_port(LIVE_SS, "9999") == []


def test_an_unparsable_endpoint_measures_nothing():
    assert mt.port_of("stdio") == "" and mt.port_of("") == ""


def test_a_url_name_gets_the_surface_and_a_host_name_the_address():
    assert mv.engine_value("SCAFFOLD_ENGINE_URL", "192.168.1.43") == "http://192.168.1.43:8000"
    assert mv.engine_value("ENGINE_IP", "192.168.1.43") == "192.168.1.43"
    assert mv.engine_value("SCAFFOLD_ENGINE_HOST", "192.168.1.43") == "192.168.1.43"
    assert mv.engine_port() == "8000", "from the engine's own configuration, not a constant"


def test_a_credential_is_never_an_address_and_other_names_are_untouched():
    assert mv.engine_value("SCAFFOLD_API_KEY", "192.168.1.43") is None
    assert mv.engine_value("RADARR_IP", "192.168.1.43") is None
    assert mv.engine_value("PALWORLD_IP", "192.168.1.43") is None


def test_nothing_is_filled_without_a_measurement():
    cmds = ["curl -s <SCAFFOLD_ENGINE_URL>/health"]
    out, _, files, notes = mv.read_from_the_engine(cmds, [], None, None)
    assert out == cmds and notes == []
    assert mv.engine_still_asked([{"name": "SCAFFOLD_ENGINE_URL"}], None) == []


def test_a_written_file_is_filled_too():
    """Unlike §17.1332's shell read, a literal address is safe inside a file --
    and ADD124's route needs it in `server.js`, not in a command."""
    files = [{"path": "/opt/control-panel-backend/routes/engine.js",
              "content": 'const ENGINE = "<SCAFFOLD_ENGINE_URL>";\n'}]
    _, _, out, notes = mv.read_from_the_engine([], [], files, "192.168.1.43")
    assert out[0]["content"] == 'const ENGINE = "http://192.168.1.43:8000";\n'
    assert len(notes) == 1 and "measured" in notes[0]["why"]


RUNBOOK = """## Run this

```bash
pct exec 111 -- bash -c "curl -s -m 5 <SCAFFOLD_ENGINE_URL>/health | head -c 40"
```

## Verify

- The panel reaches the engine: `pct exec 111 -- grep -n 'scaffold' /opt/control-panel-backend/server.js`
"""


def test_the_frame_asks_for_nothing_and_says_what_it_measured():
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": RUNNER_ENDPOINT})()
    frame = sr.frame_run(NODE, RUNBOOK, spec, POLICY, env={"profile": "root@pve"},
                         engine_address="192.168.1.43")
    assert [i["name"] for i in frame["inputs"]] == [], frame["inputs"]
    assert "http://192.168.1.43:8000/health" in " ".join(frame["commands"]), frame["commands"]
    assert any("this engine's own address, measured" in w for w in frame["engine_fixed"]), frame["engine_fixed"]
    assert not any("ENGINE_URL" in str(r.get("command", "")) for r in frame["refused"]), frame["refused"]


def test_without_a_measurement_the_frame_behaves_exactly_as_before():
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": RUNNER_ENDPOINT})()
    frame = sr.frame_run(NODE, RUNBOOK, spec, POLICY, env={"profile": "root@pve"})
    assert any(i["name"] == "SCAFFOLD_ENGINE_URL" for i in frame["inputs"]), frame["inputs"]
    assert not any("not a question for the operator" in str(r.get("why", "")) for r in frame["refused"])
