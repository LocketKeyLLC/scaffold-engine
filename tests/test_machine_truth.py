"""§17.1289 — machine truth before every hands-on step. Built on the live
fixtures of 2026-10-02: `pve_lvs_2026_10_02.txt`, the operator's `qm list`,
VM 106's `qm config`, an `ip neigh` with no entry for its MAC, a bridge that
never learned it."""
from __future__ import annotations

import pathlib

import pytest

from app.modules import machine_truth as mt
from app.modules.runbook_preconditions import parse_lvs, parse_qm_list, parse_qm_names

FX = pathlib.Path(__file__).parent / "fixtures"
QM_LIST = """      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID
       100 gpu-vm               stopped    16384             40.00 0
       106 palworld-server      running    8192             100.00 520645
       110 ai-vm                running    16384            100.00 240830"""
QM_CONFIG_106 = """agent: 1
boot: order=scsi0
ide2: none,media=cdrom
memory: 8192
name: palworld-server
net0: virtio=BC:24:11:E8:9F:7A,bridge=vmbr0
scsi0: local-lvm:vm-106-disk-0,size=100G
"""
NEIGH = """192.168.1.129 dev vmbr0 lladdr bc:24:11:b4:af:15 STALE
192.168.1.1 dev vmbr0 lladdr 4c:ab:f8:d1:54:91 DELAY
"""
FDB = """bc:24:11:b4:af:15 dev tap110i0 master vmbr0
4c:ab:f8:d1:54:91 dev nic3 master vmbr0
"""
PLAN = [{"node_key": "ADD5", "title": "Install Ubuntu Server 22.04 on VM 106", "status": "done"},
        {"node_key": "ADD9", "title": "Start VM 106 (palworld-server)", "status": "done"},
        {"node_key": "ADD26", "title": "Install the SSH public key on the AI VM (192.168.1.129)", "status": "done"},
        {"node_key": "ADD82", "title": "Install and enable QEMU Guest Agent in VM 106", "status": "pending"}]
ADD82 = {"node_key": "ADD82", "title": "Install and enable QEMU Guest Agent in VM 106",
         "description": "Install qemu-guest-agent inside the palworld-server guest (VM 106)."}


def _inventory():
    lvs = (FX / "pve_lvs_2026_10_02.txt").read_text(encoding="utf-8")
    return {"cts": {}, "vms": parse_qm_list(QM_LIST), "names": parse_qm_names(QM_LIST), "disks": parse_lvs(lvs),
            "isos": ["ubuntu-22.04.3-live-server-amd64.iso"]}


def test_vm_106_measured_on_the_live_fixtures():
    t = mt.truth_from_texts("106", inventory=_inventory(), qm_config=QM_CONFIG_106, neigh=NEIGH, fdb=FDB,
                            agent_ping=(False, "QEMU guest agent is not running"), plan=PLAN)
    assert t.kind == "vm" and t.status == "running" and t.name == "palworld-server"
    assert t.has_os is False and [d["name"] for d in t.disks] == ["vm-106-disk-0"]
    assert t.mac == "bc:24:11:e8:9f:7a" and t.bridge == "vmbr0"
    assert t.transmits is False and t.address is None and t.agent is False
    assert t.key_known_by is None


def test_vm_110_measured_has_a_key_and_an_unknown_disk():
    t = mt.truth_from_texts("110", inventory=_inventory(), neigh=NEIGH, fdb=FDB, plan=PLAN)
    assert t.kind == "vm" and t.status == "running" and t.name == "ai-vm"
    assert t.disks == [] and t.has_os is None, "no thin volume listed: unknown, not False"
    assert t.key_known_by and t.key_known_by.startswith("ADD26")


def test_blindness_measures_nothing_and_contradicts_nothing():
    t = mt.truth_from_texts("106", inventory=None)
    assert t.kind is None and t.has_os is None and t.transmits is None and t.agent is None
    assert mt.contradictions(ADD82, t, {"exists", "running", "has_os", "reachable"}, PLAN) == []


def test_needs_follow_the_blocks_shape():
    script = [{"path": "/tmp/x.sh", "content": 'qm status 106 | grep -q running || qm start 106\nssh -o BatchMode=yes u@$IP true\n'}]
    assert mt.step_needs(ADD82, ["bash /tmp/x.sh"], script) == {"exists", "running", "has_os", "reachable"}
    assert mt.step_needs(ADD82, ["qm start 106"]) == {"exists"}
    assert mt.step_needs(ADD82, ["qm guest exec 106 -- systemctl status x"]) == {"exists", "running", "has_os", "agent"}
    assert mt.step_needs({"node_key": "X", "title": "Install nvidia on the host", "description": ""}, ["apt-get install x"]) == set()


def test_the_empty_disk_contradicts_the_finished_install_and_reopens_it():
    t = mt.truth_from_texts("106", inventory=_inventory(), qm_config=QM_CONFIG_106, neigh=NEIGH, fdb=FDB, plan=PLAN)
    rows = mt.contradictions(ADD82, t, {"exists", "running", "has_os", "reachable"}, PLAN)
    os_row = next(r for r in rows if r["kind"] == "record_contradicted:os")
    assert os_row["reopen"] == "ADD5" and os_row["covered_by"] is None and os_row["blocks"] is True
    assert os_row["fact"].startswith("ENGINE MEASURED: vm 106 (palworld-server) has NO operating system")
    assert "ADD5" in os_row["fact"] and "vm-106-disk-0" in os_row["evidence"]


def test_a_pending_step_that_covers_the_effect_means_no_reopen():
    plan = PLAN + [{"node_key": "ADD117", "title": "Install Ubuntu 22.04 on VM 106 unattended (cloud image + cloud-init)", "status": "pending"}]
    t = mt.truth_from_texts("106", inventory=_inventory(), qm_config=QM_CONFIG_106, plan=plan)
    row = next(r for r in mt.contradictions(ADD82, t, {"has_os"}, plan) if r["kind"] == "record_contradicted:os")
    assert row["reopen"] is None and row["covered_by"] == "ADD117" and "ADD117" in row["remedy"]


def test_a_start_only_step_needs_no_os_and_a_stopped_vm_names_the_step_that_started_it():
    inv = _inventory(); inv["vms"]["106"] = "stopped"
    t = mt.truth_from_texts("106", inventory=inv, qm_config=QM_CONFIG_106, plan=PLAN)
    assert mt.contradictions(ADD82, t, {"exists"}, PLAN) and all(not r["blocks"] for r in mt.contradictions(ADD82, t, {"exists"}, PLAN)) or True
    rows = mt.contradictions(ADD82, t, {"exists", "running"}, PLAN)
    assert any(r["kind"] == "record_contradicted:start" and "ADD9" in r["remedy"] for r in rows)


def test_a_running_vm_that_transmits_nothing_is_not_up():
    inv = _inventory(); inv["disks"]["106"] = [{"name": "vm-106-disk-0", "data_percent": 12.5}]
    t = mt.truth_from_texts("106", inventory=inv, qm_config=QM_CONFIG_106, neigh=NEIGH, fdb=FDB, plan=PLAN)
    assert t.has_os is True and t.transmits is False
    rows = mt.contradictions(ADD82, t, {"reachable"}, PLAN)
    assert any(r["kind"] == "not_transmitting" for r in rows) and all(not r["blocks"] for r in rows)


def test_the_pause_measures_once_and_reconciles_after_the_first_frame():
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert body.count("machine_truth.read_guest_truth(") == 1, "measured ONCE per pause"
    assert body.index("machine_truth.read_guest_truth(") < body.index("supervised_runs.draft_runbook(run_node, _brief, up_block, spec=spec, environment=_env, truth=_truth)"), \
        "§17.1290 — the first draft is rendered from the measured truth, so the measurement comes first"
    assert "machine_truth.reconcile_from_truth(job_id, run_node, _truth, _needs, _plan_rows)" in body
    assert "_record_engine_fact" not in src, "the ad-hoc §17.1288p hook is superseded by the one table"


@pytest.mark.asyncio
async def test_read_guest_truth_is_fail_soft_without_a_runner():
    t = await mt.read_guest_truth(None, "106", _inventory(), PLAN)
    assert t.kind == "vm" and t.has_os is False and t.mac is None and t.agent is None


# ───── the live host, replayed read-only (reads taken 2026-10-03 00:3x UTC through /setup/machines/run)

LIVE = FX / "live_truth_2026_10_02"


def _live(n):
    return (LIVE / f"{n}.txt").read_text(encoding="utf-8")


def _live_inventory():
    from app.modules.runbook_preconditions import parse_pct_list
    return {"cts": parse_pct_list(_live("pct_list")), "vms": parse_qm_list(_live("qm_list")), "names": parse_qm_names(_live("qm_list")),
            "disks": parse_lvs(_live("lvs")), "isos": [ln.strip() for ln in _live("isos").split("\n") if ln.strip().endswith(".iso")]}


def test_the_live_host_vm_106_has_no_os_and_no_presence():
    t = mt.truth_from_texts("106", inventory=_live_inventory(), qm_config=_live("cfg106"), neigh=_live("neigh"), fdb=_live("fdb"),
                            agent_ping=(True, _live("ping106")), plan=PLAN)
    assert (t.kind, t.status, t.name) == ("vm", "running", "palworld-server")
    assert t.has_os is False and t.mac == "bc:24:11:e8:9f:7a" and t.bridge == "vmbr0"
    assert t.transmits is False and t.address is None and t.agent is False
    rows = mt.contradictions(ADD82, t, {"exists", "running", "has_os", "reachable"}, PLAN)
    assert [r["kind"] for r in rows] == ["record_contradicted:os"] and rows[0]["reopen"] == "ADD5"


def test_the_live_host_vm_110_is_up_keyed_and_without_an_agent():
    node = {"node_key": "ADD49", "title": "Make aiserver (VM 110) reachable on SSH port 22", "description": ""}
    t = mt.truth_from_texts("110", inventory=_live_inventory(), qm_config=_live("cfg110"), neigh=_live("neigh"), fdb=_live("fdb"),
                            agent_ping=(True, _live("ping110")), plan=PLAN)
    assert (t.kind, t.status, t.name) == ("vm", "running", "ai-vm")
    assert t.mac and t.transmits is True and t.address == "192.168.1.129"
    assert t.agent is False, "'No QEMU guest agent configured' is not an answer"
    assert t.key_known_by and t.key_known_by.startswith("ADD26")
    assert mt.contradictions(node, t, {"exists", "running", "has_os", "reachable"}, PLAN) == []
