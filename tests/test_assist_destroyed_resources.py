"""§17.908 — a committed step whose deliverable was destroyed.

Live (session 613dd1df): the operator ran `qm destroy 106 --purge` twice, yet
T22 "Create PalWorld VM" — the step that built VM 106 — stayed `committed` for
the rest of the session, so every later step's premise ("VM 106 exists") was
silently false.

Surface, never auto-mutate: this records a `constraint` note, which §17.677
turns into a re-plan proposal the operator approves. Silently re-opening a step
on a regex match would be the §17.891 mistake.
"""
from __future__ import annotations

import pytest

from app.modules.assist_memory import find_destroyed_resources as find

pytestmark = pytest.mark.asyncio

LIVE_1376 = ('root@pve:~# qm destroy 106 --purge\n'
             '  Logical volume "vm-106-disk-0" successfully removed.\n'
             '  Logical volume "vm-106-disk-1" successfully removed.\n'
             'purging VM 106 from related configurations..')


def test_detects_the_live_destroy():
    assert find(LIVE_1376) == [{"kind": "vm", "id": "106"}]


def test_distinguishes_vm_from_container():
    assert find("pct destroy 104") == [{"kind": "ct", "id": "104"}]
    assert find("qm destroy 106") == [{"kind": "vm", "id": "106"}]


def test_deduplicates_repeated_ids():
    assert find("qm destroy 106 --purge\nqm destroy 106 --purge") == [
        {"kind": "vm", "id": "106"}]


@pytest.mark.parametrize("benign", [
    "qm config 106",
    "qm stop 106",
    "I might destroy 106 later",          # prose, not a command
    "the destroy operation is dangerous",
    "",
])
def test_silent_on_non_destroy_text(benign):
    assert find(benign) == []


def test_surfaces_as_a_constraint_note_and_never_mutates_steps():
    """The §17.891 contract: an inference about plan state gets SURFACED for
    approval, never applied. No UPDATE of assist_steps/dag_nodes here."""
    import inspect
    from app.modules.assist_memory import flag_steps_for_destroyed_resources
    src = inspect.getsource(flag_steps_for_destroyed_resources)
    assert 'kind="constraint"' in src
    assert "dedupe=True" in src
    assert "UPDATE" not in src.upper().replace("UPDATED_AT", "")


def test_only_committed_steps_are_considered():
    """A pending step needs no warning — it has not claimed to have built
    anything yet."""
    import inspect
    from app.modules.assist_memory import flag_steps_for_destroyed_resources
    assert "s.status = 'committed'" in inspect.getsource(
        flag_steps_for_destroyed_resources)


def test_wired_into_the_unconditional_scribe():
    import inspect
    from app.modules.assist_memory import derive_turn_memory
    assert "flag_steps_for_destroyed_resources" in inspect.getsource(derive_turn_memory)
