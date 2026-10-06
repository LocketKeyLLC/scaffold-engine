"""§17.1394 — a step that names several guests and acts on none has no single subject.

Live, 2026-10-06. ADD135, "Make the machines that serve the operator's goals start
on boot", carried the measured onboot state of every guest on the host — VM 106
named first — plus the operator's decision: onboot on VM 110 ai-vm only, never VM
100 gpu-vm (one GPU cannot be passed to two running VMs).

`subject_guest` took the FIRST known id in the text. The subject came back 106, the
`run_in_vm_via_agent` template drafted a script to run INSIDE palworld-server, and
the stopped-guest rule (§17.1288f) then refused the draft with the remedy "start VM
100 first" — the VM the operator had just said must not run.

The two paths on either side already kept the rule: more than one guest acted on
returns None, and a name match must be unique. The id-mention path was the one
that did not.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules.machine_truth import subject_guest
from app.modules.runbook_templates import select_template

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add135_names_ten_guests_acts_on_none_2026_10_06.json").read_text())
INV = {"names": {"100": "gpu-vm", "101": "jellyfin", "102": "prowlarr", "103": "radarr",
                 "104": "sonarr", "105": "download-client", "106": "palworld-server",
                 "110": "ai-vm", "111": "control-panel"}}


def test_the_live_add135_has_no_single_subject():
    assert subject_guest(LIVE, INV) is None


def test_so_no_guest_template_drafts_it():
    """The template path is gated on a subject; with none, the step is drafted as
    host work, which is what editing /etc/pve/qemu-server/*.conf is."""
    assert select_template(LIVE, None) is None


def test_the_title_still_decides_when_it_names_one():
    n = {"title": "Set onboot on VM 110", "description": "VM 106 and VM 100 already have it."}
    assert subject_guest(n, INV) == "110"


def test_one_guest_named_in_the_text_is_still_the_subject():
    n = {"title": "Install the game server", "description": "Runs on VM 106 as the steam user."}
    assert subject_guest(n, INV) == "106"


def test_the_same_guest_named_twice_is_one_guest():
    n = {"title": "Fix the agent", "description": "VM 106's agent is off; enable it on VM 106."}
    assert subject_guest(n, INV) == "106"


@pytest.mark.parametrize("desc", [
    "VM 106 and VM 110 both lack it.",
    "CT 101 holds the library; container 103 reads it.",
])
def test_two_guests_named_is_no_single_subject(desc):
    assert subject_guest({"title": "Do a thing", "description": desc}, INV) is None


def test_an_id_no_machine_has_does_not_count_as_a_second_guest():
    """§17.1344 still holds: "container 999" (a uid) is not a guest, so it does not
    turn a one-guest step into a many-guest one."""
    n = {"title": "Own the tree", "description": "On CT 103, in-container 999:996 is container 999."}
    assert subject_guest(n, INV) == "103"


def test_the_fixture_is_the_live_node_and_carries_no_secret():
    assert LIVE["node_key"] == "ADD135"
    blob = json.dumps(LIVE).lower()
    for leak in ("apikey", "api_key", "token", "password"):
        assert leak not in blob
