"""§17.1459 — /etc/pve is the Proxmox host's: written there from the host, never from inside a guest.

Live (ADD151, 2026-10-10, fixture: the second drafted block): §17.1285's "a step about a guest must reach
the guest" refused the correct host-side `… | tee /etc/pve/firewall/101.fw`, and the redraft wrote every
guest's firewall file INSIDE that guest (`pct exec 101 -- sh -c 'cat > /etc/pve/firewall/101.fw …'`,
`qm guest exec 106 … /etc/pve/firewall/106.fw`) — useless there.
"""
import json
import pathlib

from app.modules import supervised_runs as sr

BLOCK2 = json.loads(pathlib.Path("tests/fixtures/add151_block2_2026_10_10.json").read_text())
NODE = {"title": "Proxmox firewall: enforce the security groups on every guest",
        "description": "turn on the firewall for CT 101 and VM 106"}


def test_the_in_guest_writes_are_refused():
    out = sr.pve_config_written_inside_a_guest(BLOCK2)
    paths = {o["why"].split(" lives on")[0] for o in out}
    assert "/etc/pve/firewall/101.fw" in paths and "/etc/pve/firewall/106.fw" in paths
    assert len(out) == 10, "all eight containers and both VMs"
    assert all("lives on the Proxmox HOST, not inside a guest" in o["why"] for o in out)


def test_a_host_side_write_reaches_the_guest_it_configures():
    host = ["printf '%s\\n' '[OPTIONS]' 'enable: 1' | tee /etc/pve/firewall/101.fw"]
    assert sr.commands_never_reach_the_guest(host, NODE) == []
    assert sr.pve_config_written_inside_a_guest(host) == []
    still = ["apt-get install -y qemu-guest-agent"]
    assert sr.commands_never_reach_the_guest(still, NODE), "a host-side install is still refused"


def test_registered_for_redraft():
    m = "lives on the Proxmox HOST, not inside a guest"
    assert m in sr._SHAPE_REFUSALS and sr._WHOSE_GAP[m] == "drafter"


def test_a_two_word_qm_subcommand_is_the_host_s_work_on_its_guest():
    """§17.1628 — job 0fb4ea7f RM1: `qm disk unlink 110 …` was refused as work that "would change the HOST instead of
    guest 110" while `qm set 110 …` beside it passed: the reach pattern knew `qm <verb> N` only."""
    from app.modules.supervised_runs import commands_never_reach_the_guest
    node = {"title": "On the Proxmox host: remove VM 110's unused disk and eject the ISO",
            "prompt_template": "On the Proxmox host (not inside the VM): remove VM 110's unused disk."}
    rm1 = ["cp /etc/pve/qemu-server/110.conf /root/110.conf.bak.$(date +%Y%m%d-%H%M%S)",
           "qm disk unlink 110 --idlist unused0 --force", "qm set 110 --ide2 none,media=cdrom",
           "qm set 110 --boot order=scsi0"]
    assert commands_never_reach_the_guest(rm1, node) == []
    for two_word in ["qm disk move 110 scsi0 oasis", "qm disk resize 110 scsi0 +10G", "qm cloudinit update 106"]:
        assert commands_never_reach_the_guest([two_word], {"title": "VM 110 disk"}) == [], two_word
    # the rule still bites: ADD82's host-side install for a guest reaches no guest
    assert commands_never_reach_the_guest(["apt-get install -y qemu-guest-agent"],
                                          {"title": "Install the guest agent in VM 106"})
