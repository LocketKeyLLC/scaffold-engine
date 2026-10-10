"""§17.1455 — a guest id inside a dotted number is not that guest.

Live (ADD150, 2026-10-10): the step's text says "Tailscale runs directly on the Proxmox host (pve,
100.84.253.90)"; `\\b100\\b` matched guest 100 (gpu-vm, stopped), and the engine drafted a script that STARTS VM
100 and ssh-es into it to advertise the route — on the wrong machine, asking for a GPU-VM account.
"""
import json
import pathlib

from app.modules import machine_truth as mt

ADD150 = json.loads(pathlib.Path("tests/fixtures/add150_node_2026_10_10.json").read_text())
INV = {"names": {"100": "gpu-vm", "101": "jellyfin", "106": "palworld-server", "110": "ai-vm",
                 "111": "control-panel", "120": "caddy-proxy", "130": "pihole"}}


def test_the_tailscale_address_is_not_vm_100():
    assert "100.84.253.90" in ADD150["description"]
    assert mt._mentions_guest(ADD150["description"], "100", "gpu-vm") is False
    assert mt.subject_guest(ADD150, INV) is None


def test_a_real_mention_still_counts():
    for t in ("Start VM 100.", "VM 100 (gpu-vm) must stay off", "guest 100, then reboot", "qm start 100"):
        assert mt._mentions_guest(t, "100", "gpu-vm") is True, t
    for t in ("version 1.100.2", "port 8100", "192.168.1.100", "10.100.0.1"):
        assert mt._mentions_guest(t, "100", "gpu-vm") is False, t
