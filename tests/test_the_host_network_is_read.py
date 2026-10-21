"""§17.1740 — the LAN a step scans is read off the host, not asked of the operator.

Live (job f9ca3ee1 T1 "Scan LAN for devices", 2026-10-21): `nmap -sn <LAN_SUBNET>`, input LAN_SUBNET asked."""
from __future__ import annotations

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr


@pytest.mark.smoke
def test_the_placeholder_becomes_the_hosts_own_read_and_guest_lines_are_left():
    cmds, verify, notes = mv.read_the_host_network(["nmap -sn <LAN_SUBNET>"], ["nmap -sn <LAN_SUBNET>"])
    assert cmds == [f"nmap -sn {mv.HOST_LAN_READ}"] and verify == cmds and notes
    guest = "pct exec 101 -- sh -c 'nmap -sn <LAN_SUBNET>'"
    assert mv.read_the_host_network([guest], [])[0] == [guest]
    assert mv.read_the_host_network(["echo <LAPTOP_IP>"], [])[0] == ["echo <LAPTOP_IP>"]
    for name in ("SUBNET", "LAN_SUBNET", "HOME_NETWORK", "LAN_CIDR"):
        assert mv.read_the_host_network([f"nmap -sn <{name}>"], [])[0] == [f"nmap -sn {mv.HOST_LAN_READ}"], name


def test_the_live_frame_asks_nothing_and_its_check_is_a_read():
    node = {"node_key": "T1", "title": "Scan LAN for devices", "description": "Run nmap on the LAN. Output devices only."}
    rb = "## Run this\n\n```bash\nnmap -sn <LAN_SUBNET>\n```\n\n## Verify\n\n- `nmap -sn <LAN_SUBNET>`\n"
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    frame = sr.frame_run(node, rb, spec, {"allow": ["ANY"], "sudo": True, "helper": "24"}, env={})
    assert frame["inputs"] == [], frame["inputs"]
    assert frame["refused"] == [], [r["why"][:200] for r in frame["refused"]]
    assert frame["commands"] == [f"nmap -sn {mv.HOST_LAN_READ}"]
