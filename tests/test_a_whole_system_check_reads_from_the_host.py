"""§17.1473 — a check of the whole build is drafted from the host, with the operator's decisions in front of it.

Live (2026-10-11): T37 "Validate entire build" (fixture) was drafted into CT 120's helper script (fixture): the host's
firewall files and Tailscale read from inside the proxy container, and Jellyfin/Radarr/Prowlarr tested at public URLs
— because its text named LXC 120 once in an old note, and still carried an EARLIER plan note ("each service is
reachable over public HTTPS") that the operator's later decision on ADD149 (fixture) overruled: only the control
panel is reachable from the internet.
"""
import json
import pathlib

from app.modules import runbook_inputs as ri
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

FIX = pathlib.Path("tests/fixtures")
T37 = json.loads((FIX / "t37_node_2026_10_11.json").read_text())
FILES = json.loads((FIX / "t37_drafted_files_2026_10_11.json").read_text())
STEPS = json.loads((FIX / "decision_steps_2026_10_11.json").read_text())


def test_the_whole_build_spans_every_guest():
    assert rt.spans_guests(T37) is True and rt.host_side_only(T37) is True

    class Truth:
        kind, agent = "ct", True
    assert not rt.RUN_IN_VM_VIA_AGENT.applies(T37, Truth())


def test_one_machines_check_still_has_its_guest():
    one = {"title": "Check Jellyfin is reachable", "description": "Inside LXC 101, `systemctl is-active jellyfin`."}
    assert rt.spans_guests(one) is False


def test_host_reads_inside_the_proxy_are_refused():
    out = sr.host_only_inside_a_guest(["bash /tmp/in_ct_120.sh start"], FILES)
    assert len(out) == 1 and "/etc/pve/firewall/groups" in out[0]["why"] and sr.HOST_ONLY_MARKER in out[0]["why"]
    assert sr.host_only_inside_a_guest(["pct exec 120 -- cat /etc/pve/firewall/cluster.fw"])
    assert sr.host_only_inside_a_guest(["cat /etc/pve/firewall/cluster.fw", "pct exec 120 -- systemctl is-active caddy"]) == []
    src = pathlib.Path("app/modules/supervised_runs.py").read_text()
    assert src.count(sr.HOST_ONLY_MARKER) == 5      # two refusals, the constant, the shape list, the whose-gap map


def test_the_operators_decisions_are_facts_newest_first():
    facts = ri.operator_decisions(STEPS)
    assert facts and all(f.startswith("Operator decision (") for f in facts)
    panel = next(f for f in facts if "ADD149" in f)
    assert "only the control panel is reachable from the internet" in panel and "where they disagree, this wins" in panel
    assert not any("(T2," in f for f in facts), "an undated 'Operator decision:' is the question, not the answer"
    assert not any("T37" in f for f in facts), "only done steps carry decisions"
