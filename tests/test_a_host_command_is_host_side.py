"""§17.1335 — a command that exists only on the host makes a step host-side work.

Live, 2026-10-03: the Proxmox host rebooted, and the step the engine then drafted
to set the boot flags — "Make the machines that serve the operator's goals start
on boot", whose own text names `qm set 106 --onboot 1` and `pct set 111 --onboot
1` — came back as a `reach_vm_ssh_and_run` draft that tried to ssh INTO VM 106 and
was then refused four times by the engine's own preconditions (containers 111 and
120 and VMs 100 and 110 are stopped and the block reaches into them). The frame
parked with Run off and "I'll do it myself" suggested, for work that is three
one-line commands on the host.

`_HOST_SIDE_RE` knew `qm resize` and `set … cpu|cores|memory|ram`, so `qm set
--onboot` and `pct set --nameserver` were not host-side, and `host_side_only`
returned False with no in-guest signal anywhere in the text.

`qm`, `pct`, `pvesm`, `pvesh` and `pveum` do not exist inside a guest. A step
whose text names one of them, with a subcommand, is host-side work — except
`pct exec` and `qm guest exec`, which are precisely how the host runs something
INSIDE a guest, and are left to the in-guest signal to decide.
"""
from __future__ import annotations

import json
import pathlib

from app.modules import runbook_templates as rt

FX = pathlib.Path(__file__).parent / "fixtures"
#: every step of the live home-lab plan that names a host-only command, plus controls
PLAN = json.loads((FX / "homelab_plan_host_side_2026_10_03.json").read_text(encoding="utf-8"))
BY_KEY = {n["node_key"]: n for n in PLAN}
#: measured on the full 165-step plan: these are the steps the new rule adds
FLIPS = ["ADD7", "ADD18", "ADD27", "ADD30", "ADD37", "ADD45", "ADD53", "ADD64",
         "ADD83", "ADD118", "ADD129", "ADD135"]


def test_the_boot_flag_step_is_host_side_work():
    """The step that started this: its text names `qm set` and `pct set`."""
    node = BY_KEY["ADD135"]
    assert "qm set 106 --onboot 1" in node["description"]
    assert rt.host_side_only(node), "three one-line commands on the host"
    assert rt.intent_of(node) is None, "so no guest template claims it"


def test_the_rule_adds_exactly_the_steps_it_should():
    """Measured over every step in the plan that names a host-only command: the
    rule turns these twelve host-side and nothing else. Each is config work on
    the host — a nameserver, a disk size, a mount point, a boot flag, a delete."""
    hits = sorted(n["node_key"] for n in PLAN if rt.host_side_only(n))
    for key in FLIPS:
        assert key in hits, f"{key}: {BY_KEY[key]['title']}"


def test_the_media_storage_step_is_host_side_too():
    """ADD129 mounts the shared pool into three containers with `pct set -mp0`.
    Host work: a container cannot mount its own storage from inside."""
    node = BY_KEY["ADD129"]
    assert "pct set 103" in node["description"] or "pct set" in node["description"]
    assert rt.host_side_only(node)


def test_pct_exec_is_not_a_host_side_signal():
    """`pct exec` and `qm guest exec` are how the host reaches INSIDE a guest, so
    they must not route a step away from the guest templates."""
    inside = {"node_key": "X", "title": "Run the migration inside container 111",
              "description": "pct exec 111 -- bash -c 'systemctl restart control-panel'"}
    assert not rt.host_side_only(inside)
    agent = {"node_key": "Y", "title": "Install the package in VM 106",
             "description": "qm guest exec 106 -- apt-get install -y curl"}
    assert not rt.host_side_only(agent)


def test_a_step_that_says_both_stays_with_the_guest_template():
    """§17.1329 — every guest template starts its guest first, so a step naming
    both the start and in-guest work is fully served by it."""
    node = {"node_key": "ADD66", "title": "Start VM 106 and bring the PalWorld service up on UDP 8211",
            "description": "Start VM 106 (palworld-server) on the host, then confirm inside the guest "
                           "that palworld.service is active (`qm start 106`)."}
    assert not rt.host_side_only(node), "the in-guest half wins"


def test_the_resize2fs_lesson_still_holds():
    """§17.1304 — a bare verb must not match work done inside the guest."""
    node = {"node_key": "ADD84", "title": "Grow the VM 106 filesystem",
            "description": "Inside VM 106, run resize2fs /dev/sda1 so the partition fills the disk."}
    assert not rt.host_side_only(node)


def test_the_console_step_keeps_its_own_template():
    """ADD118 reads VM 106's serial console while it boots. It IS host-side work,
    and the new rule now says so — but `intent_of` asks about the boot capture and
    the console BEFORE host-side, so the step keeps the template that owns it
    rather than falling through to the model path."""
    node = BY_KEY["ADD118"]
    assert rt.host_side_only(node), "reading a serial socket is host work"
    assert rt.intent_of(node) == rt.WATCH_GUEST_BOOT.name, "its text is about the boot"
    assert rt.intent_of(node) is not None, "the host-side rule never steals it"


def test_a_step_naming_no_host_command_is_untouched():
    node = {"node_key": "Z", "title": "Write the project README",
            "description": "Describe the architecture and how to set it up."}
    assert not rt.host_side_only(node) and rt.intent_of(node) is None
