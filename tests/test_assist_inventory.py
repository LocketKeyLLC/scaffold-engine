"""§17.1083 — the system map, the parser repairs, and the query tokenizer.

Every input here is the SHAPE the live session (613dd1df, 2026-09-17) held:
the state-check probe script that contaminated CT 101, the `pct list` /
`qm list` tables the engine never kept, the `ip -4 addr` paste the prose
distiller dropped, the fact that called VM 110's MAC "the Proxmox host", and
the goal line whose hostname became the search phrase "org times".
"""
from __future__ import annotations

import re

from app.modules import assist_inventory as inv
from app.modules.assist_state import merge_system_state, parse_system_state, render_system_state

PROBE_PASTE = """root@pve:~# echo "== S:T11 =="
pct config 101 | grep -E '^mp0'; pct exec 101 -- sh -c 'mountpoint -q /media && echo ok'
echo "== S:T22 =="
qm config 106
== S:T1 ==
---
      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID
       106 palworld-server      running    8192             100.00 287136
       110 ai-vm                running    16384            100.00 450514
---
VMID       Status     Lock         Name
101        running                 jellyfin
111        running                 control-panel
120        running                 caddy-proxy
---
== S:T11 ==
mp0: /oasis/media,mp=/media,ro=1
== S:T22 ==
bios: seabios
boot: order=scsi0;ide2
name: palworld-server
net0: virtio=BC:24:11:E8:9F:7A,bridge=vmbr0
scsi0: local-lvm:vm-106-disk-0,size=100G
root@pve:~# """

CONFIG_120 = """root@pve:~# pct config 120
arch: amd64
hostname: caddy-proxy
memory: 512
net0: name=eth0,bridge=vmbr0,firewall=0,hwaddr=BC:24:11:AC:C9:06,type=veth
rootfs: local-lvm:vm-120-disk-0,size=4G
root@pve:~# """

CONFIG_110 = """root@pve:~# qm config 110
name: ai-vm
net0: virtio=BC:24:11:B4:AF:15,bridge=vmbr0
scsi0: local-lvm:vm-110-disk-0,size=100G
root@pve:~# """

IPADDR_120 = """root@pve:~# pct exec 120 -- ip -4 addr show eth0 | grep inet
    inet 192.168.1.26/24 brd 192.168.1.255 scope global eth0
root@pve:~# """


def test_probe_script_echo_is_refused_and_lists_are_harvested():
    st = parse_system_state(PROBE_PASTE)
    assert "101" in st and st["101"]["source"] == "pct list"          # not the contaminated config
    assert st["101"]["attrs"] == {"hostname": "jellyfin", "status": "running", "_listed_name": "jellyfin"} and st["101"]["devices"] == {}
    assert st["106"]["attrs"]["name"] == "palworld-server" and st["106"]["kind"] == "vm"
    assert st["120"]["attrs"] == {"hostname": "caddy-proxy", "status": "running", "_listed_name": "caddy-proxy"}
    assert set(st) == {"101", "106", "110", "111", "120"}


def test_multi_command_line_with_config_last_is_still_an_echo():
    st = parse_system_state(CONFIG_120 + "\nroot@pve:~# pct status 120; pct config 120 | grep hostname\nhostname: caddy-proxy\nroot@pve:~# ")
    assert st["120"]["attrs"]["hostname"] == "caddy-proxy"
    # a key repeating means a second resource's output — the first record stops there
    two = "root@pve:~# pct config 111\narch: amd64\nhostname: control-panel\narch: amd64\nhostname: other\nroot@pve:~# "
    assert parse_system_state(two)["111"]["attrs"]["hostname"] == "control-panel"


def test_foreign_disk_refuses_the_record():
    bad = "root@pve:~# pct config 101\nhostname: jellyfin\nrootfs: local-lvm:vm-105-disk-0,size=8G\nroot@pve:~# "
    assert parse_system_state(bad) == {}


def test_ip_addr_paste_becomes_structure_and_merges_into_the_config_record():
    st = merge_system_state(parse_system_state(CONFIG_120), parse_system_state(IPADDR_120))
    assert st["120"]["attrs"]["ip"] == "192.168.1.26" and st["120"]["attrs"]["hostname"] == "caddy-proxy"
    assert "net0" in st["120"]["devices"]                                # the config read survived
    # the other order too: a config read over an ip-addr record keeps the ip
    st2 = merge_system_state(parse_system_state(IPADDR_120), parse_system_state(CONFIG_120))
    assert st2["120"]["attrs"]["ip"] == "192.168.1.26" and st2["120"]["devices"]
    host = parse_system_state("root@pve:~# cat /etc/network/interfaces | grep -A3 vmbr0\nauto vmbr0\niface vmbr0 inet static\n    address 192.168.1.156/24\n    gateway 192.168.1.1\nroot@pve:~# ")
    assert host["host"]["attrs"]["ip"] == "192.168.1.156"
    assert "host" not in render_system_state(host)                       # the map renders the host, not this block


def _env():
    state = {}
    for paste in (PROBE_PASTE, CONFIG_120, CONFIG_110, IPADDR_120):
        state = merge_system_state(state, parse_system_state(paste))
    return {
        "system_state": state,
        "substitutions": {"JELLYFIN_IP": "192.168.1.20", "PROWLARR_IP": "192.168.1.21"},
        "facts": [
            "Proxmox host vmbr0 is configured static 192.168.1.156/24, gateway 192.168.1.1, bridge-ports nic3.",
            "Container listeners: CT 101 on 0.0.0.0:8096, CT 111 on *:3001, CT 120 on *:443 and *:80; CT 105 has no listener.",
            "The ACME challenge for defrusciohomelab.duckdns.org fails with 'Timeout during connect' against 67.240.32.243.",
            "Spectrum router (SAX1V1K) WAN IPv4 is 67.240.32.243.",
            "VM 106 (palworld-server) status is stopped; VM 110 (ai-vm) status is running.",
            "The Proxmox host with MAC prefix b4:af:15 has IP address 192.168.1.129.",
            "No vm-100 LVs exist, confirming VM 100 is fully removed.",
            "From the Proxmox host, curl returns HTTP 302 for 192.168.1.20:8096 and 404 for 192.168.1.25:3001.",
            "On the Proxmox host, 'ip neigh | grep 192.168.1.26' returns no entry.",
        ],
    }


def test_map_joins_id_name_ip_mac_ports_and_status():
    sm = inv.build_system_map(_env())
    m = sm["machines"]
    assert m["120"]["name"] == "caddy-proxy" and list(m["120"]["ips"]) == ["192.168.1.26"]
    assert m["120"]["macs"] == ["BC:24:11:AC:C9:06"] and m["120"]["ports"] == ["80", "443"]
    assert m["101"]["name"] == "jellyfin" and list(m["101"]["ips"]) == ["192.168.1.20"]     # pinned by name
    assert m["106"]["status"] == "stopped" and m["110"]["status"] == "running"                # clause-wise, the later fact wins
    assert "100" not in m and sm["gone"] == ["100"]
    # a curl target / neighbour lookup never becomes a machine's address
    assert list(sm["host"]["ips"]) == ["192.168.1.156", "192.168.1.129"]
    assert "192.168.1.25" not in sm["host"]["ips"] and "192.168.1.26" not in sm["host"]["ips"]


def test_map_names_the_entry_point_and_the_ingress_path():
    sm = inv.build_system_map(_env())
    assert sm["ingress"] == {"domain": "defrusciohomelab.duckdns.org", "wan": "67.240.32.243",
                             "entry_id": "120", "entry_ip": "192.168.1.26", "upstreams": {}}
    text = inv.render_system_map(_env())
    assert "router port-forward TCP 80,443 → 192.168.1.26 = CT 120 (caddy-proxy), MAC BC:24:11:AC:C9:06" in text
    assert "Never forward 22 or 8006" in text


def test_map_flags_the_host_ip_conflict_and_resolves_the_mac_fragment():
    sm = inv.build_system_map(_env())
    kinds = [c["kind"] for c in sm["conflicts"]]
    assert "host_ip" in kinds and "mac_owner" in kinds
    host_c = next(c for c in sm["conflicts"] if c["kind"] == "host_ip")
    assert host_c["resolutions"] == [{"fragment": "b4:af:15", "id": "110", "name": "ai-vm"}]
    text = inv.render_system_map(_env())
    assert "recorded at 192.168.1.156" in text and "AND 192.168.1.129" in text
    assert "b4:af:15 in that fact belongs to VM 110 (ai-vm), not to the host" in text
    assert inv.resolve_mac_fragments("An the Proxmox b4:af:15's ip is 192.168.1.129", _env()) == [
        {"fragment": "b4:af:15", "id": "110", "name": "ai-vm", "kind": "vm"}]
    assert inv.resolve_mac_fragments("nothing here", _env()) == []


def test_map_is_the_first_thing_after_the_pins_in_the_environment_block():
    from app.modules.assist_render import render_environment_block
    block = render_environment_block(_env())
    assert block.index("### SYSTEM MAP") < block.index("### Known facts about the operator's system")
    assert block.index("- JELLYFIN_IP = 192.168.1.20") < block.index("### SYSTEM MAP")
    assert render_system_state({}) == "" and inv.render_system_map({}) == ""
    assert inv.render_system_map({"facts": ["nothing about machines"]}) == ""


def test_goal_keywords_never_carry_hostnames_ips_or_urls():
    from app.modules.assist_research_lib import _goal_keywords
    kws = _goal_keywords("External HTTPS to https://defrusciohomelab.duckdns.org times out from cellular; host at 192.168.1.26", 6)
    assert "org times" not in kws and not any("duckdns" in k or "192.168" in k for k in kws)
    assert "times out" in kws


def test_map_reaches_both_injection_paths(monkeypatch):
    """§17.751's lesson, again: two renderers, one edit. The live engine runs
    the unified path (ASSIST_UMEM_INJECT=true); the map must lead the state
    tier there AND in the legacy block."""
    from app.config import settings
    from app.modules.assist_render import _render_memory_or_legacy, render_session_memory
    env = _env()
    uni = render_session_memory(env, None, budget=20000)
    assert "### SYSTEM MAP" in uni and uni.index("### SYSTEM MAP") < uni.index("CONFIRMED resource CONFIGURATION")
    assert "192.168.1.26 = CT 120 (caddy-proxy)" in uni
    monkeypatch.setattr(settings, "assist_unified_memory_enabled", False)
    legacy = "\n\n".join(_render_memory_or_legacy(env, None))
    assert "### SYSTEM MAP" in legacy
    monkeypatch.setattr(settings, "assist_unified_memory_enabled", True)
    monkeypatch.setattr(settings, "assist_umem_inject", True)
    assert "### SYSTEM MAP" in "\n\n".join(_render_memory_or_legacy(env, None))


# ── §17.1083b — the ingress gate ────────────────────────────────────────────

LIVE_2743 = ("You need to pick the device whose MAC matches your Palworld server.\n\n## Find your server's MAC\n"
             "1. On the Proxmox host run `qm config 106 | grep net0`.\n2. If VM 106 isn't the right device, run `ip neigh | grep 192.168.1.26`.\n"
             "## Select it in the Spectrum app\n1. Under **Port Forwarding**, find the device whose MAC matches.\n"
             "2. Tap that device, then continue adding your port assignment (e.g., UDP 8211 for Palworld).\n"
             "3. Set a **DHCP reservation** for that MAC so it always gets 192.168.1.26.")
LIVE_2762 = ("1. **Reserve the Proxmox host's IP first** — find your Proxmox host (MAC `b4:af:15`, IP `192.168.1.129`) and toggle **Reserved IP**.\n"
             "2. **Add the port assignment** — for the Proxmox web interface use External port: `8006`, Internal port: `8006`, Protocol: `TCP`.\n"
             "For SSH, add a second assignment: External port: `22`, Internal port: `22`.")
GOOD = ("The device to forward to is CT 120 (caddy-proxy) at 192.168.1.26, MAC BC:24:11:AC:C9:06. In the Spectrum app "
        "find that MAC under Port Forwarding & IP Reservations, reserve 192.168.1.26 for it, then add TCP 80 and TCP 443.")
GAME = "At the Palworld step: forward UDP 8211 to VM 106 (palworld-server). Add a port assignment for UDP 8211 in the Spectrum app."


def test_ingress_gate_hits_the_two_live_answers_and_passes_the_right_ones():
    sm = inv.build_system_map(_env())
    assert inv.ingress_issues(LIVE_2743, sm) == [{"kind": "wrong_machine", "id": "106", "name": "palworld-server"}]
    kinds = sorted(i["port"] for i in inv.ingress_issues(LIVE_2762, sm) if i["kind"] == "mgmt_port")
    assert kinds == ["22", "8006"]
    assert inv.ingress_issues(GOOD, sm) == []
    assert inv.ingress_issues(GAME, sm) == []                       # a game-server forward at its own step is fine
    assert inv.ingress_issues("nothing about the network", sm) == []
    assert inv.ingress_issues(LIVE_2743, None) == [] and inv.ingress_issues(LIVE_2743, {"ingress": {}, "machines": {}}) == []
    notice = inv.ingress_notice(inv.ingress_issues(LIVE_2743, sm), sm)
    assert "CT 120 (caddy-proxy) at 192.168.1.26, MAC BC:24:11:AC:C9:06, TCP ports 80 and 443" in notice and "NOT VM 106 (palworld-server)" in notice
    foot = inv.ingress_footer(inv.ingress_issues(LIVE_2762, sm), sm)
    assert "Wrong target" in foot and "forwards management port 8006" in foot and "CT 120 (caddy-proxy)" in foot


import pytest


@pytest.mark.asyncio
async def test_verify_answer_regenerates_on_an_ingress_violation_then_annotates(monkeypatch):
    from app.config import settings
    from app.modules import assist_evidence as ev
    monkeypatch.setattr(settings, "assist_answer_verification_enabled", True)
    monkeypatch.setattr(settings, "assist_answer_verification_regenerate", True)
    sm = inv.build_system_map(_env())
    seen = {}
    async def regen_good(notice):
        seen["notice"] = notice; return GOOD
    out, rep = await ev.verify_answer(LIVE_2743, sources=[], corpus=LIVE_2743 + GOOD, node_key="T37", label="assist_research",
                                      regenerate=regen_good, topology=sm)
    assert "INGRESS NOTICE" in seen["notice"] and "NOT VM 106" in seen["notice"]
    assert out.startswith("The device to forward to is CT 120") and rep["regenerated"] is True and rep["ingress"] == []
    async def regen_bad(notice):
        return LIVE_2762
    out2, rep2 = await ev.verify_answer(LIVE_2743, sources=[], corpus=LIVE_2743 + LIVE_2762, node_key="T37", label="assist_research",
                                        regenerate=regen_bad, topology=sm)
    assert rep2["regenerated"] is False and "⚠️ **Wrong target**" in out2 and "VM 106 (palworld-server)" in out2
    # no topology → the gate is inert and nothing else changes
    out3, rep3 = await ev.verify_answer(GOOD, sources=[], corpus=GOOD, node_key="T37", label="x", regenerate=None, topology=None)
    assert rep3["ingress"] == [] and "Wrong target" not in out3


def test_every_verify_answer_call_site_passes_the_topology():
    import pathlib, re
    root = pathlib.Path(inv.__file__).resolve().parents[2]
    for f in ("assist_guide.py", "assist_research_lib.py", "assist_agent.py"):
        src = (root / "app" / "modules" / f).read_text(encoding="utf-8")
        calls = len(re.findall(r"await (?:assist_guide\.)?(?:verify_answer|research_one)\(", src))
        assert calls == src.count("topology="), f


# ── §17.1084 — invariants at every write; facts reconciled before they are stored ──

from app.modules.assist_state import reconcile_system_state

CORRUPT_101 = {"kind": "ct", "source": "pct config 101",
               "attrs": {"hostname": "prowlarr", "name": "palworld-server", "bios": "seabios",
                         "rootfs": "local-lvm:vm-105-disk-0,size=8G", "_listed_name": "jellyfin"},
               "devices": {"net0": "virtio=BC:24:11:E8:9F:7A,bridge=vmbr0", "scsi0": "local-lvm:vm-106-disk-0,size=100G",
                           "unused0": "oasis:vm-110-disk-1"}}
VM_106 = {"kind": "vm", "source": "qm config 106", "attrs": {"name": "palworld-server"},
          "devices": {"net0": "virtio=BC:24:11:E8:9F:7A,bridge=vmbr0", "scsi0": "local-lvm:vm-106-disk-0,size=100G"}}


def test_the_live_corrupted_record_cannot_be_stored_whatever_produced_it():
    clean, repairs = reconcile_system_state({"101": CORRUPT_101, "106": VM_106})
    assert clean["101"] == {"kind": "ct", "source": "pct config 101", "attrs": {"hostname": "jellyfin", "_listed_name": "jellyfin"}, "devices": {}}
    assert clean["106"]["devices"]["net0"] == "virtio=BC:24:11:E8:9F:7A,bridge=vmbr0"       # the real owner keeps its MAC
    assert {r["kind"] for r in repairs} == {"foreign_disk", "name_mismatch", "wrong_kind_key", "wrong_kind_net"}
    again, more = reconcile_system_state(clean)
    assert again == clean and more == []                                               # idempotent
    # merge and parse both pass through it
    assert merge_system_state({"106": VM_106}, {"101": CORRUPT_101})["101"]["devices"] == {}
    assert "scsi0" not in parse_system_state("root@pve:~# pct config 101\nhostname: jellyfin\nscsi0: local-lvm:vm-106-disk-0\nroot@pve:~# ").get("101", {}).get("devices", {})


def test_a_shared_mac_between_two_config_reads_of_the_right_form_drops_both():
    a = {"kind": "vm", "source": "qm config 106", "attrs": {"name": "a"}, "devices": {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0"}}
    b = {"kind": "vm", "source": "qm config 107", "attrs": {"name": "b"}, "devices": {"net0": "virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0"}}
    clean, repairs = reconcile_system_state({"106": a, "107": b})
    assert clean["106"]["devices"] == {} and clean["107"]["devices"] == {}
    assert all(r["kind"] == "shared_mac" and r["kept_on"] is None for r in repairs)


def test_invariants_hold_for_any_table():
    """Property: whatever records go in, the table that comes out has unique
    MACs, disks that name their owner, no field of the other kind's form, and
    a second pass changes nothing."""
    import random
    rnd = random.Random(1083)
    kinds = ["ct", "vm"]
    for _ in range(300):
        ids = [str(rnd.randint(100, 130)) for _ in range(rnd.randint(1, 6))]
        table = {}
        for rid in ids:
            kind = rnd.choice(kinds)
            mac = f"BC:24:11:{rnd.randint(0,255):02X}:{rnd.randint(0,255):02X}:{rnd.choice(['01','02','03']):s}"
            form = rnd.choice(["virtio=", "name=eth0,hwaddr="])
            disk_owner = rnd.choice(ids + [rid, rid])
            table[rid] = {"kind": kind, "source": rnd.choice([f"pct config {rid}", f"qm config {rid}", "pct list", "x"]),
                          "attrs": {k: "v" for k in rnd.sample(["hostname", "name", "bios", "arch", "memory", "status", "ip"], rnd.randint(0, 4))}
                                   | ({"_listed_name": "listed"} if rnd.random() < 0.3 else {}),
                          "devices": {"net0": f"{form}{mac},bridge=vmbr0", "scsi0": f"local-lvm:vm-{disk_owner}-disk-0"}}
        clean, _ = reconcile_system_state(table)
        macs = [m for rec in clean.values() for v in rec["devices"].values() for m in inv._MAC_RE.findall(str(v))]
        assert len(macs) == len(set(m.upper() for m in macs)), "a MAC on two machines"
        for rid, rec in clean.items():
            for v in list(rec["devices"].values()) + list(rec["attrs"].values()):
                m = re.search(r"\bvm-(\d+)-", str(v))
                assert m is None or m.group(1) == rid, "a foreign disk survived"
            if rec["kind"] == "ct":
                assert not ({"name", "bios"} & set(rec["attrs"])) and not any(str(v).startswith("virtio=") for v in rec["devices"].values())
            if rec["kind"] == "vm":
                assert not ({"hostname", "arch"} & set(rec["attrs"])) and not any("hwaddr=" in str(v) for v in rec["devices"].values())
            if rec["attrs"].get("_listed_name"):
                assert (rec["attrs"].get("hostname") or rec["attrs"].get("name")) == rec["attrs"]["_listed_name"]
        again, more = reconcile_system_state(clean)
        assert again == clean and more == []


def test_a_fact_is_reconciled_against_the_records_before_it_is_stored():
    env = _env()
    fact = "The Proxmox host with MAC prefix b4:af:15 has IP address 192.168.1.129."
    stored, update = inv.reconcile_fact(fact, env)
    assert stored == ("The Proxmox host with MAC prefix b4:af:15 has IP address 192.168.1.129 "
                      "[engine: MAC b4:af:15 is VM 110 (ai-vm) per its config, not the Proxmox host; so 192.168.1.129 is VM 110 (ai-vm)'s address].")
    assert update == {"110": {"kind": "vm", "attrs": {"ip": "192.168.1.129"}, "devices": {}, "source": "fact reconciled (b4:af:15)"}}
    # consistent facts pass untouched; an address they state still becomes structure
    assert inv.reconcile_fact("VM 110 (ai-vm) net0 MAC BC:24:11:B4:AF:15 is at 192.168.1.129", env) == (
        "VM 110 (ai-vm) net0 MAC BC:24:11:B4:AF:15 is at 192.168.1.129",
        {"110": {"kind": "vm", "attrs": {"ip": "192.168.1.129"}, "devices": {}, "source": "fact reconciled (BC:24:11:B4:AF)"}})
    assert inv.reconcile_fact("Operator completed port forwarding for the Proxmox host.", env) == ("Operator completed port forwarding for the Proxmox host.", None)
    assert inv.reconcile_fact("anything", {}) == ("anything", None)


def test_set_environment_routes_new_facts_through_reconcile():
    import pathlib
    src = (pathlib.Path(inv.__file__).resolve().parents[0] / "assist_environment.py").read_text(encoding="utf-8")
    block = src[src.index("    if facts:"):src.index("        # Cap: keep the most recent")]
    assert "reconcile_fact(t, current)" in block and "merge_system_state" in block and "assist_fact_reconciled" in block


def test_a_reconciled_fact_no_longer_binds_the_host():
    env = _env()
    env["facts"] = [inv.reconcile_fact(f, env)[0] for f in env["facts"]]
    sm = inv.build_system_map(env)
    assert list(sm["host"]["ips"]) == ["192.168.1.156"]
    assert not [c for c in sm["conflicts"] if c["kind"] in ("host_ip", "mac_owner")]
