"""§17.1469 — a step whose commands are all the host's own tools is host-side work.

Live (2026-10-11): ADD154 (fixture) names `qm set 106 --ipconfig0 ip=dhcp`, `qm cloudinit update 106`, `qm reboot 106`
as its work, and mentions "the Palworld service starts on boot" and a `qm guest exec 106 -- …` check. The in-guest
words won: the three host commands were drafted into the in-VM agent template, to run inside VM 106.
"""
import json
import pathlib

import pytest

from app.modules import runbook_templates as rt

NODE = json.loads(pathlib.Path("tests/fixtures/add154_host_side_node_2026_10_11.json").read_text())


def test_the_live_step_is_host_side():
    assert rt.host_side_only(NODE) is True


@pytest.mark.parametrize("text", [
    "Inside VM 106 install nginx: `apt-get install -y nginx`. Done when `systemctl is-active nginx` answers.",
    "Start VM 106 and bring the PalWorld service up on UDP 8211.",
    "Copy the file in: `pct push 111 /tmp/x /opt/x`, then `pct exec 111 -- systemctl restart panel`.",
    "Start VM 106 (palworld-server) on the host, then confirm inside the guest that palworld.service is active "
    "(`qm start 106`).",
])
def test_guest_work_is_not(text):
    assert rt.host_side_only({"title": "x", "description": text}) is False


def test_the_vm_agent_template_steps_aside():
    class Truth:
        kind, agent = "vm", True
    assert rt.RUN_IN_VM_VIA_AGENT.applies(NODE, Truth()) is False
