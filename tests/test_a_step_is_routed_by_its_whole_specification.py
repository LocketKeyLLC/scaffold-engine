"""§17.1609 — a step is routed host-side or in-guest by its whole specification, not its title alone.

Live (job 0fb4ea7f, 2026-10-15): the planner wrote each step's spec in `prompt_template` and left `description` empty.
T3 "Expand ai-vm disk" ("Grow VM 110's disk from 100 GB to 2 TB …") was drafted as growpart/resize2fs INSIDE the guest
(the next step's work) instead of the host's `qm resize`; T4 "Increase VM 110's RAM …" matched no host-side verb; T14
"Inside VM 110: docker compose up … Stops at all containers running" read as whole-lab work and went host-side.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import runbook_templates as rt

STEPS = {n["node_key"]: n for n in json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "server_move_steps_2026_10_15.json").read_text())}


@pytest.mark.parametrize("key,host", [("T3", True), ("T4", True), ("T5", False), ("T14", False)])
def test_the_live_steps_route_where_their_work_is(key, host):
    assert not STEPS[key].get("description"), "the live shape: the spec is in prompt_template only"
    assert rt.host_side_only(STEPS[key]) is host


@pytest.mark.parametrize("text", ["Increase VM 110's RAM to 48 GB", "Expand ai-vm RAM",
                                  "raise the memory of VM 106"])
def test_raising_a_guests_ram_or_cores_is_host_work(text):
    assert rt._HOST_SIDE_RE.search(text), text


@pytest.mark.parametrize("text", ["increase PHP memory limit", "increase the memory_limit in php.ini"])
def test_an_application_memory_limit_is_not(text):
    assert not rt._HOST_SIDE_RE.search(text), text


def test_the_whole_lab_phrase_still_means_the_lab_when_no_guest_is_named():
    assert rt.spans_guests({"title": "Check all services are reachable"})
    assert not rt.spans_guests({"title": "Deploy", "prompt_template": "Inside VM 110: compose up; all containers running"})
