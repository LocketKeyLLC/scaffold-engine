"""§17.910 — a bare VM that nothing installs an OS on.

The live homelab plan (job 55f68b7f) contained both shapes side by side:

    T25 Create AI VM       → T26 Install AI VM OS → T28 Install NVIDIA drivers
    T22 Create PalWorld VM → T23 Install PalWorld server

No node anywhere in that plan installed an operating system on VM 106, so
T23's premise — a booted, reachable Ubuntu — was never established and no step
was ever going to establish it. The operator spent three days installing an OS
that was not in the plan.
"""
from __future__ import annotations

import pytest

from app.modules.dag_generator import insert_missing_os_install as fix

pytestmark = pytest.mark.asyncio


def _t(i, name, deps=(), desc=""):
    return {"id": i, "name": name, "description": desc, "depends_on": list(deps),
            "tool": "shell", "type": "action", "inputs": [], "outputs": []}


def test_repairs_the_live_palworld_gap():
    tasks = [_t("T22", "Create PalWorld VM", ["T7"]),
             _t("T23", "Install PalWorld server", ["T22"])]
    added = fix(tasks)
    assert added == ["T22_OS"]
    new = next(t for t in tasks if t["id"] == "T22_OS")
    assert new["name"] == "Install OS on PalWorld VM"
    assert new["depends_on"] == ["T22"]
    # the dependent now waits on the OS, not on the empty VM shell
    assert next(t for t in tasks if t["id"] == "T23")["depends_on"] == ["T22_OS"]


def test_leaves_the_ai_branch_alone_it_already_had_its_step():
    """The same plan got this right one branch over — a no-op there is the
    proof the pass is repairing an asymmetry, not rewriting every plan."""
    tasks = [_t("T25", "Create AI VM", ["T7"]),
             _t("T26", "Install AI VM OS", ["T25"]),
             _t("T28", "Install NVIDIA drivers", ["T26"])]
    assert fix(tasks) == []
    assert len(tasks) == 3


def test_containers_are_exempt():
    """`pct create` clones a distro template, so an LXC has a userspace the
    moment it exists. Only a bare VM needs an OS installed into it. Inserting
    an OS step for every container would be noise on every media-stack plan."""
    for name in ("Create Radarr LXC container", "Create CT 104 for Sonarr",
                 "Provision the download-client container"):
        tasks = [_t("A", name), _t("B", "Install Radarr", ["A"])]
        assert fix(tasks) == [], name


def test_no_in_guest_dependents_means_no_step_needed():
    """A VM created only to be documented or verified externally needs no OS."""
    tasks = [_t("A", "Create bastion VM"), _t("B", "Record the VM id in the notes", ["A"])]
    assert fix(tasks) == []


def test_is_idempotent():
    """Second run finds the OS step it inserted and does nothing."""
    tasks = [_t("T22", "Create PalWorld VM"), _t("T23", "Install PalWorld server", ["T22"])]
    assert fix(tasks) == ["T22_OS"]
    assert fix(tasks) == []
    assert len([t for t in tasks if t["id"].startswith("T22_OS")]) == 1


def test_generated_id_never_collides():
    tasks = [_t("T22", "Create PalWorld VM"),
             _t("T22_OS", "Some unrelated pre-existing node"),
             _t("T23", "Install PalWorld server", ["T22"])]
    added = fix(tasks)
    assert added == ["T22_OS2"]


def test_only_in_guest_dependents_are_rewired():
    """A sibling that acts on the VM from OUTSIDE (a host-side firewall rule)
    must keep depending on the VM's creation, not on its OS."""
    tasks = [_t("A", "Create game VM"),
             _t("B", "Install the game server", ["A"]),
             _t("C", "Note the VM id for the inventory", ["A"])]
    fix(tasks)
    by = {t["id"]: t for t in tasks}
    assert by["B"]["depends_on"] == ["A_OS"]
    assert by["C"]["depends_on"] == ["A"]


def test_wired_into_generation_behind_a_valve():
    import inspect
    from app.config import settings
    from app.modules import dag_generator
    assert settings.dag_os_install_gap_check_enabled is True
    src = inspect.getsource(dag_generator)
    assert "insert_missing_os_install(normalized)" in src
    assert "dag_os_install_gap_check_enabled" in src
