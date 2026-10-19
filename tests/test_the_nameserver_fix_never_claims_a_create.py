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


def test_a_step_whose_text_only_mentions_a_nameserver_is_not_claimed():
    """§17.1675 — live, SIP2 ("VM 106: make 192.168.1.44 its static address") became `pct reboot 111`"""
    sip2 = {"node_key": "SIP2", "title": "VM 106: make 192.168.1.44 its static address",
            "prompt_template": ("Make VM 106's address static ... nameserver 192.168.1.1 ... Done when ... from CT 111 "
                                "`ssh -o BatchMode=yes aedefruscio@192.168.1.44 sudo -n systemctl is-active palworld.service`")}
    inv = {"cts": {"111": "running"}, "vms": {"106": "running"}}
    c, v, notes = rewrite_set_container_nameserver(sip2, ["qm set 106 --ipconfig0 ip=192.168.1.44/24"], [], None, inv)
    assert notes == [] and c == ["qm set 106 --ipconfig0 ip=192.168.1.44/24"]
    vm = {"title": "Give VM 106 a working nameserver"}
    assert rewrite_set_container_nameserver(vm, ["x"], [], None, inv)[2] == []
    ct = {"title": "Give container 111 a working nameserver"}
    assert rewrite_set_container_nameserver(ct, ["x"], [], None, inv)[2]


SIP2_TEXT = "Make VM 106's address static at the address it has now, 192.168.1.44/24, gateway 192.168.1.1, nameserver 192.168.1.1 (search domain tail3692cc.ts.net, as before). On the Proxmox host: `qm set 106 --ipconfig0 ip=192.168.1.44/24,gw=192.168.1.1` (the nameserver is already set) so every boot renders it. Inside VM 106 now, without a reboot: keep a dated copy of /etc/netplan/50-cloud-init.yaml, write it with the same static address, routes and nameservers (matching MAC bc:24:11:e8:9f:7a, set-name eth0), mode 600, and `netplan apply` -- the address does not change, so the game and the panel stay connected. Do NOT reboot VM 106 (friends may be playing). Done when: inside VM 106 `ip -4 addr show eth0` shows 192.168.1.44/24 without `dynamic`, `ip -4 route` shows the default via 192.168.1.1 without `proto dhcp`; on the host `qm cloudinit dump 106 network` shows the static address; `cat /etc/ssh/ssh_host_ed25519_key.pub` inside VM 106 equals what SIP1 printed; and from CT 111 `ssh -o BatchMode=yes aedefruscio@192.168.1.44 sudo -n systemctl is-active palworld.service` prints active."


def test_the_live_text_alone_never_claims_it():
    """§17.1675 — each guard on its own: SIP2's real text under a title that names no VM is still not claimed"""
    inv = {"cts": {"111": "running"}, "vms": {"106": "running"}}
    node = {"title": "Make the Palworld address static", "prompt_template": SIP2_TEXT}
    assert rewrite_set_container_nameserver(node, ["x"], [], None, inv)[2] == []


def test_a_step_that_sets_a_guests_proxmox_settings_gets_no_guest_template():
    """§17.1676 — `qm set 106 …` is host work; the in-VM template would run it inside VM 106"""
    from app.modules import runbook_templates as rt
    node = {"title": "Proxmox: make VM 106's cloud-init address static 192.168.1.44",
            "prompt_template": "On the Proxmox host only: `qm set 106 --ipconfig0 ip=192.168.1.44/24,gw=192.168.1.1`, "
                               "then `qm cloudinit update 106`."}
    assert rt._HOST_ADMIN_RE.search(node["prompt_template"])
    for t in ("pct set 109 --nameserver 1.1.1.1", "qm cloudinit dump 106 network"):
        assert rt._HOST_ADMIN_RE.search(t), t
    assert not rt._HOST_ADMIN_RE.search("netplan set ethernets.eth0.dhcp4=false")


def test_a_netplan_write_that_matches_cloud_init_is_allowed():
    """§17.1677 — the same static address Proxmox now renders is not "undone at the next boot\""""
    from app.modules.rewrite_guard import _agrees_with_cloud_init
    static = "ipconfig0: ip=192.168.1.44/24,gw=192.168.1.1"
    plan = ("network:\n  version: 2\n  ethernets:\n    eth0:\n      addresses:\n      - 192.168.1.44/24\n"
            "      routes:\n      - to: default\n        via: 192.168.1.1\n")
    assert _agrees_with_cloud_init(static, plan)
    assert not _agrees_with_cloud_init(static, plan.replace("192.168.1.44/24", "192.168.1.50/24"))
    assert not _agrees_with_cloud_init(static, "eth0:\n  dhcp4: true\n")
    assert _agrees_with_cloud_init("ipconfig0: ip=dhcp", "eth0:\n  dhcp4: true\n")
    assert not _agrees_with_cloud_init("ipconfig0: ip=dhcp", plan)
