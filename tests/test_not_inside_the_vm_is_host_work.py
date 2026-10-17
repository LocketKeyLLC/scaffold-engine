"""§17.1627 — "not inside the VM" says the work is NOT in the guest.

Live (job 0fb4ea7f RM1, 2026-10-17): "On the Proxmox host (not inside the VM): … `qm disk unlink 110 --idlist unused0
--force`, `qm set 110 --ide2 none,media=cdrom`, `qm set 110 --boot order=scsi0`" matched the in-guest signal
`\\binside\\b`, which vetoed both host-side rules; the host's commands were drafted into the in-guest ssh template and
refused (`/etc/pve/qemu-server/110.conf` is not in a guest).
"""
from __future__ import annotations

from app.modules import runbook_templates as rt

RM1 = {"title": "On the Proxmox host: remove VM 110's unused disk and eject the ISO", "description": "From LOOK1.",
       "prompt_template": (
           "On the Proxmox host (not inside the VM), the operator's decision of 2026-10-17: remove VM 110's unused disk "
           "and take the installer ISO out of its CD drive; VM 110 keeps running.\n"
           "1. Copy /etc/pve/qemu-server/110.conf to /root first.\n"
           "2. `qm disk unlink 110 --idlist unused0 --force`.\n3. `qm set 110 --ide2 none,media=cdrom`.\n"
           "4. `qm set 110 --boot order=scsi0`.\n"
           "Done when `qm config 110` shows no unused0, ide2 none and boot order=scsi0.")}


def test_the_rm1_step_is_host_side():
    assert rt.host_side_only(RM1)
    assert rt.intent_of(RM1) is None, "no guest template"
    assert not rt.REACH_VM_SSH_AND_RUN.applies(RM1, None)


def test_a_negated_inside_is_removed_and_an_affirmed_one_is_kept():
    for neg in ["not inside the VM", "never inside a guest", "nothing in the container", "not run inside VM 110"]:
        assert "inside" not in rt._affirmed(neg) and " in the container" not in rt._affirmed(neg), neg
    for pos in ["then confirm inside the guest that palworld.service is active", "inside VM 106"]:
        assert rt._affirmed(pos) == pos


def test_work_that_is_really_inside_stays_inside():
    # §17.1329's ADD66 shape: a host start AND an in-guest check — the guest template serves it
    add66 = {"title": "Start VM 106 and confirm inside the guest that palworld.service is active",
             "prompt_template": "Start VM 106, then confirm inside the guest that palworld.service is active."}
    assert not rt.host_side_only(add66)
    # a host step that says what it is NOT, next to real in-guest work, is still in-guest work
    mixed = {"title": "VM 110: restart nginx",
             "prompt_template": "Not on the Proxmox host: inside VM 110 run `systemctl restart nginx`."}
    assert not rt.host_side_only(mixed)
