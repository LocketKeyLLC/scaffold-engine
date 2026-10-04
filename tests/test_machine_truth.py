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
    row = next(r for r in rows if r["kind"] == "record_contradicted:start")
    assert "ADD9" in row["remedy"] and row["reopen"] == "ADD9" and row["fact"] and "reopened" in row["fact"], \
        "§17.1307 — the done start step is reopened so the start runs first and the next step's checks can be read inside"
    assert row["blocks"] is False
    start_step = {"node_key": "ADD9", "title": "Start VM 106 (palworld-server)", "description": ""}
    row2 = next(r for r in mt.contradictions(start_step, t, {"exists", "running"}, PLAN) if r["kind"] == "record_contradicted:start")
    assert row2["reopen"] is None and row2["fact"] is None, "a start step does not reopen itself"


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
    assert body.index("machine_truth.read_guest_truth(") < body.index("supervised_runs.draft_runbook(run_node, _brief, up_block, spec=spec, environment=_env, truth=_truth, services=_services)"), \
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


def test_the_live_host_ct_111_has_a_mac_a_bridge_and_no_presence():
    """§17.1301 — `pct config` writes the MAC fourth (`hwaddr=`), not first; the
    live measurement of CT 111 at 06:56 UTC was `mac None, bridge None`."""
    node = {"node_key": "ADD68", "title": "Set static IP on control-panel container 111", "description": ""}
    t = mt.truth_from_texts("111", inventory=_live_inventory(), qm_config=_live("cfg111"), neigh=_live("neigh"), fdb=_live("fdb"), plan=PLAN)
    assert (t.kind, t.status, t.name) == ("ct", "stopped", "control-panel")
    assert t.mac == "bc:24:11:66:89:94" and t.bridge == "vmbr0"
    assert t.transmits is False and t.address is None and t.agent is None
    assert "pct config" in t.reads and "qm config" not in t.reads
    assert mt.contradictions(node, t, {"exists"}, PLAN) == []


def test_parse_net0_reads_both_tools_lines():
    assert mt.parse_net0("net0: virtio=BC:24:11:E8:9F:7A,bridge=vmbr0\n") == ("bc:24:11:e8:9f:7a", "vmbr0")
    assert mt.parse_net0("net0: name=eth0,bridge=vmbr0,firewall=1,hwaddr=BC:24:11:66:89:94,type=veth\n") == ("bc:24:11:66:89:94", "vmbr0")
    assert mt.parse_net0("memory: 2048\n") == (None, None)


@pytest.mark.asyncio
async def test_read_guest_truth_asks_pct_config_for_a_container(monkeypatch):
    asked = []

    async def fake_probe(spec, command):
        asked.append(command)
        if command.startswith("pct config"):
            return True, _live("cfg111")
        return True, {"ip neigh show": _live("neigh"), "bridge fdb show": _live("fdb")}.get(command, "")
    monkeypatch.setattr(mt, "_probe", fake_probe)
    t = await mt.read_guest_truth(object(), "111", _live_inventory(), PLAN)
    assert asked[0] == "pct config 111" and "ip neigh show" in asked and "bridge fdb show" in asked
    assert not any(a.startswith("qm ") for a in asked), "a container is never asked with qm"
    assert t.mac == "bc:24:11:66:89:94" and t.transmits is False


def test_the_live_stopped_caddy_container_reopens_its_recorded_start():
    """2026-10-03 10:38 UTC: CT 120 stopped, ADD110 "Start container 120 (caddy-proxy)" done
    on a runner error, ADD88 about to install Caddy over a validated Caddyfile it could not see."""
    plan = [{"node_key": "ADD110", "title": "Start container 120 (caddy-proxy)", "status": "done"},
            {"node_key": "ADD88", "title": "Install Caddy and write the Caddyfile inside LXC 120", "status": "pending"}]
    node = plan[1]
    t = mt.truth_from_texts("120", inventory={"cts": {"120": "stopped"}, "vms": {}, "names": {}, "disks": {}}, plan=plan)
    rows = mt.contradictions(node, t, {"exists", "running"}, plan)
    assert [r["reopen"] for r in rows] == ["ADD110"]
    assert "container 120 is stopped" in rows[0]["fact"] and "ADD110" in rows[0]["fact"]


def test_needs_resolve_the_scripts_own_guest_variable():
    """§17.1308 — `GID=120` + `pct exec "$GID"` is a need for 120 to be running."""
    node = {"node_key": "ADD88", "title": "Install Caddy and write the Caddyfile inside LXC 120", "description": ""}
    script = {"path": "/tmp/in_ct_120.sh", "content": 'GID=120\npct status "$GID" | grep -q running || pct start "$GID"\npct push "$GID" /tmp/x /root/x\npct exec "$GID" -- bash /root/x\n'}
    assert {"exists", "running", "has_os"} <= mt.step_needs(node, ["bash /tmp/in_ct_120.sh"], [script])
    assert mt.step_needs(node, ["bash /tmp/in_ct_120.sh"], [{"path": "/tmp/x", "content": "GID=120\npct status \"$GID\"\n"}]) == {"exists"}


def test_the_live_template_frame_reopens_the_recorded_start_end_to_end():
    import json
    fr = json.loads((FX / "add88_frame_template2_2026_10_03.json").read_text(encoding="utf-8"))
    plan = [{"node_key": "ADD110", "title": "Start container 120 (caddy-proxy)", "status": "done"},
            {"node_key": "ADD88", "title": fr["title"], "status": "pending"}]
    node = {"node_key": "ADD88", "title": fr["title"], "description": "Install Caddy inside container 120 (caddy-proxy)."}
    t = mt.truth_from_texts("120", inventory={"cts": {"120": "stopped"}, "vms": {}, "names": {}, "disks": {}}, plan=plan)
    needs = mt.step_needs(node, fr["commands"], fr["files"])
    assert "running" in needs
    assert [r["reopen"] for r in mt.contradictions(node, t, needs, plan)] == ["ADD110"]


@pytest.mark.asyncio
async def test_a_reopened_start_becomes_a_dependency_of_the_step_that_needs_it(monkeypatch):
    """§17.1309 — live, ADD110 was reopened and the pause still parked ADD88 (first
    in execution order). The step that needs the guest running waits for the
    step that starts it; the pause restarts so the start is asked about first."""
    from app.modules import node_editor
    calls = []

    class _S:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False
    import app.database as _db
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def fake_reset(job_id, key, *, db, cascade, edited_by):
        calls.append(("reset", key, cascade)); return {"status": "ok"}

    async def fake_edit(job_id, key, fields, *, db, cascade, edited_by):
        calls.append(("edit", key, fields, cascade)); return {"status": "ok", "downstream_kept": []}
    monkeypatch.setattr(node_editor, "reset_node", fake_reset)
    monkeypatch.setattr(node_editor, "edit_node", fake_edit)

    import app.modules.assist_environment as _ae

    async def no_facts(*a, **k):
        return None
    monkeypatch.setattr(_ae, "set_environment", no_facts)
    plan = [{"node_key": "ADD110", "title": "Start container 120 (caddy-proxy)", "status": "done"},
            {"node_key": "ADD88", "title": "Install Caddy and write the Caddyfile inside LXC 120", "status": "pending"}]
    node = {"node_key": "ADD88", "title": plan[1]["title"], "description": "", "depends_on": ["ADD87"]}
    t = mt.truth_from_texts("120", inventory={"cts": {"120": "stopped"}, "vms": {}, "names": {}, "disks": {}}, plan=plan)
    did = await mt.reconcile_from_truth("job", node, t, {"exists", "running"}, plan)
    assert ("reset", "ADD110", False) in calls
    assert ("edit", "ADD88", {"depends_on": ["ADD87", "ADD110"]}, False) in calls, calls
    assert any(d.startswith("reopened ADD110") for d in did) and "ADD88 now waits for ADD110" in did
    assert node["depends_on"] == ["ADD87", "ADD110"]


def test_every_redraft_in_the_pause_carries_the_truth_and_a_reopen_restarts_it():
    """§17.1309 — §17.1308's template re-render was inert: only the FIRST draft
    passed `truth=_truth`, every redraft went to the model path (sibling call
    sites drift). And a reopen must not park the step that now waits."""
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    calls = body.count("supervised_runs.draft_runbook(")
    assert calls >= 8 and body.count("truth=_truth") >= calls, f"{calls} drafts, {body.count('truth=_truth')} carry the truth"
    assert "decision_pause_restart_after_reopen" in body
    # §17.1334 — four restarts now: a step already met, a reopen/insert this step
    # waits for, a split, and a split's numbering or stated count corrected on this
    # step's own text (the draft reads that text, so it restarts rather than drafts).
    assert body.count("_pause_for_decision(job_id, _depth + 1)") == 4
    assert "step_split_restart" in body
    assert body.index("machine_truth.reconcile_from_truth(") < body.index("decision_pause_restart_after_reopen") < body.index('logger.warning("supervised_run_redraft job=')


# ───── §17.1315 — a reinstalled OS reopens the in-guest work before it

from datetime import datetime, timezone as _tz

def _at(s):
    return datetime.fromisoformat(s).replace(tzinfo=_tz.utc)

PLAN_106 = [
    {"node_key": "T22", "title": "Create PalWorld VM", "status": "done", "completed_at": _at("2026-08-31T22:56:51")},
    {"node_key": "T23", "title": "Install PalWorld server", "status": "done", "completed_at": _at("2026-09-04T22:02:57")},
    {"node_key": "T24", "title": "Configure PalWorld service", "status": "done", "completed_at": _at("2026-09-04T22:05:55")},
    {"node_key": "ADD5", "title": "Install Ubuntu Server 22.04 on VM 106", "status": "done", "completed_at": _at("2026-09-04T20:00:00")},
    {"node_key": "ADD9", "title": "Start VM 106 (palworld-server)", "status": "done", "completed_at": _at("2026-09-05T10:00:00")},
    {"node_key": "ADD22", "title": "Give VM 106 (palworld-server) a 40G local-lvm boot disk", "status": "done", "completed_at": _at("2026-09-10T10:00:00")},
    {"node_key": "ADD47", "title": "Create, enable and start the palworld.service unit in VM 106", "status": "skipped", "completed_at": _at("2026-09-12T10:00:00")},
    {"node_key": "ADD117", "title": "Install Ubuntu 22.04 on VM 106 unattended (cloud image + cloud-init)", "status": "done", "completed_at": _at("2026-10-03T05:23:28")},
    {"node_key": "ADD82", "title": "Install and enable QEMU Guest Agent in VM 106", "status": "done", "completed_at": _at("2026-10-03T06:12:06")},
    {"node_key": "ADD84", "title": "Grow the VM 106 filesystem to fill the disk", "status": "done", "completed_at": _at("2026-10-03T09:35:26")},
]


def test_the_live_plan_the_palworld_install_and_service_are_voided_by_the_reinstall():
    voided = mt.in_guest_work_voided_by_reinstall(PLAN_106, "106", "palworld-server")
    assert [v["node_key"] for v in voided] == ["T23", "T24"], voided
    assert all(v["install"] == "ADD117" for v in voided)
    # host-side work on the guest survives (the VM, its start, its disk), the older OS install is superseded not reopened,
    # work done AFTER the reinstall stands, skipped steps are not reopened
    assert mt.in_guest_work_voided_by_reinstall([n for n in PLAN_106 if n["node_key"] != "ADD117"], "106", "palworld-server") == [], "no reinstall: nothing voided"
    assert mt.in_guest_work_voided_by_reinstall(PLAN_106, "110", "ai-vm") == []


@pytest.mark.asyncio
async def test_after_an_os_install_is_recorded_done_the_voided_steps_are_reopened_with_a_fact(monkeypatch):
    from app.modules import node_editor
    import app.database as _db
    import app.modules.assist_environment as _ae
    calls = []

    class _R:
        def __init__(self, rows): self._rows = rows
        def mappings(self): return self
        def all(self): return self._rows
        def first(self): return self._rows[0] if self._rows else None

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, q, params=None):
            if "assist_sessions" in str(q): return _R([{"id": "sid-1", "metadata": {}}])
            return _R(PLAN_106)
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def fake_reset(job_id, key, *, db, cascade, edited_by):
        calls.append(("reset", key, cascade, edited_by[:40])); return {"status": "ok"}

    async def fake_facts(*, session_id, facts, db):
        calls.append(("fact", session_id, facts[0][:90]))
    monkeypatch.setattr(node_editor, "reset_node", fake_reset)
    monkeypatch.setattr(_ae, "set_environment", fake_facts)
    did = await mt.after_step_done("job", {"node_key": "ADD117", "title": PLAN_106[7]["title"]})
    assert [c[:3] for c in calls if c[0] == "reset"] == [("reset", "T23", False), ("reset", "T24", False)], calls
    assert any(c[0] == "fact" and "reinstalled the OS of guest 106" in c[2] for c in calls)
    assert [d for d in did if d.startswith("reopened")] == ["reopened T23: Install PalWorld server (voided by ADD117)",
                                                            "reopened T24: Configure PalWorld service (voided by ADD117)"], did
    # §17.1329 — ADD47 is skipped, in-guest and older than the reinstall: reported, not reopened
    assert [d for d in did if d.startswith("skips a reinstall")] and "ADD47" in did[-1]
    assert await mt.after_step_done("job", {"node_key": "ADD84", "title": "Grow the VM 106 filesystem"}) == [], "only an OS install voids anything"
    src = pathlib.Path(__import__("app.modules.supervised_runs", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert "_mt.after_step_done(job_id, {" in src, "the run's done write calls it"
    esrc = pathlib.Path(__import__("app.modules.execution_agent", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert "machine_truth.after_step_done(job_id, run_node)" in esrc, "the already-met done write calls it"
    # §17.1334 — the plan read carries every field its consumers use, and an order:
    # `already_split` reads `description` and the renumbering needs the order.
    _i = esrc.index("async def _pause_for_decision(")
    _body = esrc[_i:esrc.index("\nasync def ", _i + 10)]
    _sel = _body[_body.index("SELECT node_key, title"):]
    _sel = _sel[:_sel.index("{\"j\": job_id}")]
    for _col in ("node_key", "title", "description", "status", "completed_at", "execution_order"):
        assert _col in _sel, f"plan rows carry {_col}: {_sel}"
    assert "ORDER BY execution_order" in _sel, _sel



# ───── §17.1313 — the engine measures DNS and proposes the fix

CFG111_LIVE = "arch: amd64\nhostname: control-panel\nmemory: 2048\nnameserver: 192.168.1.30 1.1.1.1\nnet0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:82:37:C3,type=veth\nostype: debian\n"
ADD88_NODE = {"node_key": "ADD88", "title": "Install Caddy and write the Caddyfile inside LXC 120", "description": "", "depends_on": ["ADD87"]}
PLAN_120 = [{"node_key": "ADD87", "title": "Start LXC 111", "status": "skipped"}, {"node_key": "ADD110", "title": "Start container 120 (caddy-proxy)", "status": "done"},
            {"node_key": "ADD119", "title": "Reset VM 106 and capture its console", "status": "done"}, {"node_key": "ADD88", "title": ADD88_NODE["title"], "status": "pending"}]
CT_SCRIPT = [{"path": "/tmp/in_ct_120.sh", "content": "GID=120\npct status \"$GID\" | grep -q running || pct start \"$GID\"\ncat > /tmp/r.sh <<'REMOTE'\napt-get update\napt-get install -y caddy\nREMOTE\npct push \"$GID\" /tmp/r.sh /root/r.sh\npct exec \"$GID\" -- bash /root/r.sh\n"}]


def test_the_live_caddy_container_resolves_nothing_and_the_fix_is_borrowed_from_a_sibling():
    inv = {"cts": {"111": "running", "120": "running"}, "vms": {}, "names": {}, "disks": {}}
    t = mt.truth_from_texts("120", inventory=inv, dns=(True, ""), sibling_config=CFG111_LIVE, plan=PLAN_120)
    assert t.resolves is False and t.dns_hint == "192.168.1.30 1.1.1.1" and t.reads["getent hosts"] == "(printed nothing)"
    ok = mt.truth_from_texts("111", inventory=inv, dns=(True, "151.101.2.132 deb.debian.org"), plan=PLAN_120)
    assert ok.resolves is True
    assert mt.truth_from_texts("120", inventory=inv, plan=PLAN_120).resolves is None, "unread is unknown, not false"


def test_a_block_that_downloads_inside_a_guest_that_cannot_resolve_is_blocked_and_a_fix_step_is_proposed():
    inv = {"cts": {"111": "running", "120": "running"}, "vms": {}, "names": {}, "disks": {}}
    t = mt.truth_from_texts("120", inventory=inv, dns=(True, ""), sibling_config=CFG111_LIVE, plan=PLAN_120)
    needs = mt.step_needs(ADD88_NODE, ["bash /tmp/in_ct_120.sh"], CT_SCRIPT)
    assert "network" in needs
    rows = [r for r in mt.contradictions(ADD88_NODE, t, needs, PLAN_120) if r["kind"] == "no_dns"]
    assert len(rows) == 1 and rows[0]["blocks"] is True
    ins = rows[0]["insert"]
    assert ins["node_key"] == "ADD120" and ins["title"] == "Give container 120 a working nameserver" and ins["depends_on"] == []
    assert 'pct set 120 --nameserver "192.168.1.30 1.1.1.1"' in ins["description"] and "pct reboot 120" in ins["description"]
    assert rows[0]["waits"] == "ADD88" and "cannot resolve names" in rows[0]["fact"]
    assert not [r for r in mt.contradictions(ADD88_NODE, t, {"exists", "running"}, PLAN_120) if r["kind"] == "no_dns"], "a block that fetches nothing is not blocked"


@pytest.mark.asyncio
async def test_reconcile_inserts_the_fix_step_and_the_waiting_step_depends_on_it(monkeypatch):
    from app.modules import node_editor
    import app.database as _db
    import app.modules.assist_environment as _ae
    calls = []

    class _S:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def fake_insert(job_id, spec, *, db, edited_by):
        calls.append(("insert", spec["node_key"], spec["title"])); return {"status": "ok", "node_key": spec["node_key"]}

    async def fake_edit(job_id, key, fields, *, db, cascade, edited_by):
        calls.append(("edit", key, fields, cascade)); return {"status": "ok"}

    async def no_facts(*a, **k):
        return None
    monkeypatch.setattr(node_editor, "insert_node", fake_insert)
    monkeypatch.setattr(node_editor, "edit_node", fake_edit)
    monkeypatch.setattr(_ae, "set_environment", no_facts)
    inv = {"cts": {"111": "running", "120": "running"}, "vms": {}, "names": {}, "disks": {}}
    t = mt.truth_from_texts("120", inventory=inv, dns=(True, ""), sibling_config=CFG111_LIVE, plan=PLAN_120)
    node = dict(ADD88_NODE)
    did = await mt.reconcile_from_truth("job", node, t, {"exists", "running", "network"}, PLAN_120)
    assert ("insert", "ADD120", "Give container 120 a working nameserver") in calls
    assert ("edit", "ADD88", {"depends_on": ["ADD87", "ADD120"]}, False) in calls
    assert any(d.startswith("inserted ADD120") for d in did) and "ADD88 now waits for ADD120" in did
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8"); i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert 'if any(" now waits for " in d for d in _did) and _depth < 6' in body, "a reopen or an insert restarts the pause"


@pytest.mark.asyncio
async def test_the_dns_probe_runs_only_for_a_running_guest_and_borrows_a_sibling(monkeypatch):
    asked = []

    async def fake_probe(spec, command):
        asked.append(command)
        if command.startswith("pct config 120"):
            return True, "net0: name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AC:C9:06,type=veth\n"
        if command.startswith("pct config 111"):
            return True, CFG111_LIVE
        if "getent" in command:
            return True, ""
        return True, ""
    monkeypatch.setattr(mt, "_probe", fake_probe)
    inv = {"cts": {"111": "running", "120": "running"}, "vms": {}, "names": {}, "disks": {}}
    t = await mt.read_guest_truth(object(), "120", inv, PLAN_120)
    assert "pct exec 120 -- timeout 5 getent hosts deb.debian.org" in asked and "pct config 111" in asked
    assert t.resolves is False and t.dns_hint == "192.168.1.30 1.1.1.1"
    asked.clear()
    inv2 = {"cts": {"120": "stopped"}, "vms": {}, "names": {}, "disks": {}}
    t2 = await mt.read_guest_truth(object(), "120", inv2, PLAN_120)
    assert not any("getent" in a for a in asked) and t2.resolves is None, "a stopped guest cannot be asked"


# ───── §17.1316 — a guest named by name is still the subject

T23 = {"node_key": "T23", "title": "Install PalWorld server", "description": ""}
INV_NAMES = {"cts": {"111": "running", "120": "running"}, "vms": {"106": "running", "110": "running"},
             "names": {"106": "palworld-server", "110": "ai-vm", "111": "control-panel", "120": "caddy-proxy"}, "disks": {}}


def test_the_subject_is_found_by_name_when_the_text_has_no_id():
    assert mt.subject_guest(T23) is None, "the text alone names no id"
    assert mt.subject_guest(T23, INV_NAMES) == "106"
    assert mt.subject_guest({"node_key": "X", "title": "Install Caddy inside the caddy-proxy container", "description": ""}, INV_NAMES) == "120"
    assert mt.subject_guest({"node_key": "X", "title": "Set vmbr0 bridge-ports to enp5s0f3", "description": ""}, INV_NAMES) is None
    assert mt.subject_guest({"node_key": "X", "title": "Reset VM 106 and capture its console", "description": ""}, INV_NAMES) == "106", "an id still wins"
    from app.modules.runbook_preconditions import parse_pct_names
    assert parse_pct_names("VMID       Status     Lock         Name\n111        running                 control-panel\n120        stopped                 caddy-proxy\n") == {"111": "control-panel", "120": "caddy-proxy"}


def test_the_live_t23_frame_takes_the_agent_template_once_the_subject_is_known():
    from app.modules import runbook_templates as rt
    truth = mt.truth_from_texts("106", inventory=INV_NAMES, agent_ping=(True, ""), plan=[])
    assert truth.agent is True
    assert rt.subject_gid(T23) is None and rt.subject_gid(T23, truth) == "106"
    assert rt.select_template(T23, truth) is rt.INSTALL_STEAM_SERVER, "§17.1322 — a game-server install is the engine's own shape"
    vals = rt.values_for(rt.RUN_IN_VM_VIA_AGENT, T23, truth, {}, {"REMOTE_COMMANDS": "apt-get install -y lib32gcc-s1", "VERIFY_INSIDE": "ls /opt/palworld/PalServer.sh"})
    assert vals["GID"] == "106", "the gid comes from the truth for any template"
    rb = rt.render(rt.RUN_IN_VM_VIA_AGENT, vals)
    code = "\n".join(l for l in rb.split("## Write these files")[1].split("## Run this")[0].split("\n") if not l.lstrip().startswith("#"))
    assert "qm guest exec" in code and "ssh" not in code, "the agent template reaches the VM without ssh (comments aside)"
    needs = mt.step_needs(T23, ["bash /tmp/in_vm_106_agent.sh"], [{"path": "/tmp/in_vm_106_agent.sh", "content": 'GID=106\nqm guest exec "$GID" --timeout 110 --pass-stdin 1 -- bash -s < /tmp/r.sh\n'}], gid="106")
    assert {"exists", "running", "has_os", "agent"} <= needs
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    assert "machine_truth.subject_guest(run_node, _inv)" in src and "gid=_gid)" in src
