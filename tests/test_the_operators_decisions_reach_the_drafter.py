"""§17.1477 — the operator's decisions reach the drafter's prompt, after the step's text, and outrank it.

Live (2026-10-11): §17.1473 made each dated "Operator decision" a fact in `job_environment`, but `build_base_prompt`
carried only the environment's `profile`. T37 (fixture) was drafted again testing Jellyfin, Radarr, Sonarr and Prowlarr
at public URLs: its text still held the earlier note "each service is reachable over public HTTPS" — rendered under
"this wins" — and ADD149's decision (fixture) that only the control panel is public never reached the prompt.
"""
import json
import pathlib

from app.modules import prompt_assembly as pa
from app.modules import runbook_inputs as ri

FIX = pathlib.Path("tests/fixtures")
T37 = json.loads((FIX / "t37_node_2026_10_11.json").read_text())
STEPS = json.loads((FIX / "decision_steps_2026_10_11.json").read_text())


def test_the_decisions_are_in_the_prompt_after_the_steps_text():
    env = {"profile": "", "facts": ri.operator_decisions(STEPS) + ["LXC 103 (radarr) is running"]}
    prompt = pa.build_base_prompt(T37, {}, env)
    head = "THE OPERATOR'S DECISIONS"
    assert head in prompt and prompt.index("WHAT THIS STEP MUST DO") < prompt.index(head)
    block = prompt[prompt.index(head):]
    assert "only the control panel is reachable from the internet" in block and "the DECISION wins" in block
    assert "LXC 103 (radarr) is running" not in block, "only decisions, not every fact"


def test_no_decisions_no_section():
    assert "THE OPERATOR'S DECISIONS" not in pa.build_base_prompt(T37, {}, {"facts": ["x"]})


def test_a_block_that_only_reads_is_its_own_check():
    """§17.1477 — T37's 25 reads (fixture, verbatim) were refused "this block has no check at all"."""
    from app.modules import supervised_runs as sr
    cmds = json.loads((FIX / "t37_read_block_2026_10_11.json").read_text())
    assert len(cmds) >= 20 and all(sr._reads_only(c) for c in cmds), [c for c in cmds if not sr._reads_only(c)]
    assert not sr._reads_only("systemctl restart caddy") and not sr._reads_only("curl -X POST http://x")


def test_a_network_address_is_not_a_machine():
    """§17.1477 — T37's "192.168.1.0/24" was offered as <PANEL_IP>."""
    assert ri._step_ips("advertising 192.168.1.0/24 subnet routes; Caddy at 192.168.1.127", []) == ["192.168.1.127"]


def test_a_guess_from_the_text_does_not_stop_a_measurement(monkeypatch):
    """§17.1477 — <PANEL_IP> carried two scraped guesses, so CT 111 was never measured."""
    import asyncio

    from app.modules import runbook_discovery as rd
    reads = {"pct list": "VMID Status Lock Name\n111 running control-panel\n120 running caddy-proxy\n",
             "pct exec 111 -- hostname -I": "192.168.1.25 2603:7081::1\n"}

    async def read(spec, cmd):
        return reads.get(cmd, "")
    monkeypatch.setattr(rd, "_read", read)

    class Spec:
        name = "pve-runner"
    ins = [{"name": "PANEL_IP", "value": "", "secret": False,
            "suggestions": [{"value": "100.84.253.90", "source": "the step's own text", "confidence": "step"}]},
           {"name": "CADDY_IP", "value": "", "secret": False,
            "suggestions": [{"value": "192.168.1.127", "source": "system map: ct 120", "confidence": "map"}]}]
    out = asyncio.run(rd.discover_inputs(ins, [], Spec()))
    assert out[0]["value"] == "192.168.1.25", out[0]
    assert out[1]["value"] == "" and out[1]["suggestions"][0]["value"] == "192.168.1.127", "the map stays ahead"
