"""§17.1664 — the §17.1519 nameserver repair never claims the step that CREATES the container (job 3cc24343 T2)."""
from __future__ import annotations

from app.modules.supervised_runs import rewrite_set_container_nameserver

T2 = {"node_key": "T2", "title": "Create CT 109 container",
      "prompt_template": "pct create 109 … --nameserver \"192.168.1.1 1.1.1.1\" … then pct start 109."}
INV = {"cts": {"105": "running", "107": "running"}, "vms": {}, "names": {}}


def _claimed(node, inv=INV):
    _, _, notes = rewrite_set_container_nameserver(node, ["x"], [], [], inv)
    return bool(notes)


def test_a_create_step_is_left_to_its_draft():
    assert not _claimed(T2)


def test_a_repair_of_a_guest_that_does_not_exist_is_left_alone():
    assert not _claimed({"title": "Give container 109 a working nameserver"})


def test_the_repair_still_claims_an_existing_guest():
    assert _claimed({"title": "Give container 107 a working nameserver"})
