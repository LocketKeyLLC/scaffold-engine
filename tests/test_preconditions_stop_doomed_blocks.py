"""§17.1213 — do not run a block the machine already contradicts.

Two steps ran on the operator's host while the engine had a read-only channel to
it, and both were doomed before they were sent:

    ADD21  pct exec 111 -- pm2 start 1  ->  exited 255: container '111' not running!
    ADD82  pct exec 106 -- apt-get …    ->  106 is a VM, not a container

One `pct list` + `qm list` answers both. The engine had been reading that host
all evening and ran them anyway, then reported the failures as though the
machine had surprised it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pathlib as pathlib_mod
import pytest

from app.modules import runbook_preconditions as pc

# verbatim from the operator's host
PCT_LIST = """VMID       Status     Lock         Name
101        running                 jellyfin
102        running                 prowlarr
111        stopped                 control-panel
120        stopped                 dmz"""

QM_LIST = """      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID
       100 gpu-vm               stopped    16384             40.00 0
       106 palworld-server      running    8192               0.00 339509
       110 ai-vm                stopped    16384            100.00 0"""


def _spec():
    s = MagicMock(); s.name = "pve-runner"; return s


def _host(pct=PCT_LIST, qm=QM_LIST):
    async def fake(spec, tool, args):
        r = MagicMock(); r.structured = None; r.is_error = False
        r.text = {"pct list": pct, "qm list": qm}.get(args["command"], "")
        return r
    return fake


# ── the parsers, on real output ──────────────────────────────────────────

def test_the_listings_parse_and_skip_their_headers():
    assert pc.parse_pct_list(PCT_LIST) == {"101": "running", "102": "running",
                                           "111": "stopped", "120": "stopped"}
    assert pc.parse_qm_list(QM_LIST) == {"100": "stopped", "106": "running", "110": "stopped"}


def test_guests_are_read_off_the_commands():
    assert pc.guests_in(["pct exec 111 -- pm2 start 1", "qm set 106 --scsi0 x"]) == [
        ("pct", "exec", "111"), ("qm", "set", "106")]


# ── the two that actually ran ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add21_a_stopped_container_is_refused_and_the_fix_is_named():
    plan = [{"node_key": "ADD50", "title": "Start container 111 (control-panel)", "status": "pending"}]
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        out = await pc.unmet(["pct exec 111 -- pm2 start 1"], _spec(), plan=plan)
    assert len(out) == 1
    why = out[0]["why"]
    assert "container 111 is stopped" in why, why
    assert "ADD50" in why and "has not run yet" in why, "the refusal must name the step that fixes it"


@pytest.mark.asyncio
async def test_a_reboot_of_a_stopped_guest_is_refused_with_start_as_the_remedy():
    """§17.1300 — live, ADD68 wrote `pct set 111 --net0 …` then `pct reboot 111`
    on a STOPPED container. `reboot` was not a verb that needed the guest
    running, so the doomed command passed. (The test PR #678 shipped without
    is this one — added with §17.1301.)"""
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        out = await pc.unmet(["pct set 111 --net0 name=eth0,bridge=vmbr0,ip=192.168.1.25/24,gw=192.168.1.1",
                              "pct reboot 111", "pct status 111"], _spec())
    assert [r["command"] for r in out] == ["pct reboot 111"], out
    why = out[0]["why"]
    assert "container 111 is stopped" in why and "cannot reboot" in why, why
    assert "`pct start 111`" in why, "the remedy is a start, which also applies the new config"


@pytest.mark.asyncio
async def test_the_needs_running_refusal_feeds_the_redraft():
    """§17.1301 — the same frame, parked: `shape_retry_note` is what decides
    whether the chain redrafts or hands the operator a greyed-out button. The
    needs-running refusal text was not in the registry, so live the engine
    refused its own block and then parked on it (06:56 UTC, no redraft event)."""
    from app.modules.supervised_runs import shape_retry_note
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        out = await pc.unmet(["pct reboot 111"], _spec())
    note = shape_retry_note({"refused": out, "commands": ["pct reboot 111"]})
    assert note, "a machine-raised refusal the drafter can fix must redraft, not park"
    assert "pct start 111" in note, note


@pytest.mark.asyncio
async def test_add82_the_wrong_tool_for_that_guest_is_refused():
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        out = await pc.unmet(["pct exec 106 -- apt-get update"], _spec())
    assert len(out) == 1
    why = out[0]["why"]
    assert "106 is a VM on this host, not a container" in why, why
    assert "qm exec" in why or "qm guest exec" in why, "say what WOULD address it"


@pytest.mark.asyncio
async def test_the_mirror_case_is_caught_too():
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        out = await pc.unmet(["qm start 111"], _spec())
    assert "111 is a container on this host, not a VM" in out[0]["why"]


@pytest.mark.asyncio
async def test_a_guest_that_does_not_exist_is_refused():
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        out = await pc.unmet(["pct exec 999 -- true"], _spec())
    assert "no guest 999 on this host" in out[0]["why"]


# ── it must not over-refuse ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_running_container_passes():
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        assert await pc.unmet(["pct exec 101 -- systemctl status jellyfin"], _spec()) == []


@pytest.mark.asyncio
async def test_a_stopped_guest_is_fine_for_a_verb_that_does_not_need_it_running():
    """`pct start 111` is exactly what you run on a stopped container."""
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        assert await pc.unmet(["pct start 111"], _spec()) == []
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        assert await pc.unmet(["qm set 110 --boot order=scsi0"], _spec()) == []


@pytest.mark.asyncio
async def test_an_unreadable_host_refuses_nothing():
    """It must never invent a blocker out of its own blindness."""
    with patch("app.modules.mcp_client.call_tool", new=_host(pct="", qm="")):
        assert await pc.unmet(["pct exec 111 -- true"], _spec()) == []
    with patch("app.modules.mcp_client.call_tool", new=AsyncMock(side_effect=RuntimeError("down"))):
        assert await pc.unmet(["pct exec 111 -- true"], _spec()) == []


@pytest.mark.asyncio
async def test_no_runner_no_checks():
    assert await pc.unmet(["pct exec 111 -- true"], None) == []


@pytest.mark.asyncio
async def test_commands_naming_no_guest_are_left_alone():
    with patch("app.modules.mcp_client.call_tool", new=_host()):
        assert await pc.unmet(["apt-get update", "systemctl restart caddy"], _spec()) == []


# ── wired so Run is actually off ─────────────────────────────────────────

def test_a_precondition_joins_refused_so_run_is_not_offered():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert "list(preconditions or [])" in src, "preconditions must join refused"
    assert src.index("list(preconditions or [])") < src.index("if cmds and not refused"), \
        "they must be in before options/suggested are computed, or Run is still offered"


def test_the_check_runs_before_the_frame_is_built():
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea)
    assert "runbook_preconditions import read_inventory, unmet" in src
    # §17.1288f — the inventory is read once, every draft is judged against it
    assert src.index("_inv = await read_inventory(spec)") < src.index("preconditions=await _pre_for(")
    assert "preconditions=_pre)" not in src, "a draft framed against the FIRST draft's preconditions"
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert body.count("preconditions=await _pre_for(") == body.count("supervised_runs.frame_run("), \
        "every frame_run in the pause must carry its own draft's preconditions"
    assert "files=_f, node=run_node, inventory=_inv" in body


# ───── §17.1288f/g — the live ADD82 script: VM 106 stopped and never started; ssh with no key of ours

QM_LIST_106_STOPPED = QM_LIST.replace("106 palworld-server      running    8192               0.00 339509",
                                      "106 palworld-server      stopped    8192             100.00 0")
ADD82 = {"node_key": "ADD82", "title": "Install and enable QEMU Guest Agent in VM 106",
         "description": "Install qemu-guest-agent inside the palworld-server guest (VM 106) and confirm it answers."}
PLAN = [{"node_key": "ADD26", "title": "Install the SSH public key on the AI VM (192.168.1.129)", "status": "done"},
        {"node_key": "ADD57", "title": "Start VM 106 (palworld-server)", "status": "skipped"}]
RUN = 'MASS_PASSWORD="$MASS_PASSWORD" PALWORLD_USER="<PALWORLD_USER>" bash /tmp/install_agent_106.sh'


def _script(name="add82_install_agent_106_v2.sh"):
    import pathlib
    return pathlib.Path(__file__).parent.joinpath("fixtures", name).read_text(encoding="utf-8")


def _files(content):
    return [{"path": "/tmp/install_agent_106.sh", "content": content}]


@pytest.mark.asyncio
async def test_the_live_script_is_refused_twice_before_it_is_sent():
    with patch("app.modules.mcp_client.call_tool", new=_host(qm=QM_LIST_106_STOPPED)):
        out = await pc.unmet([RUN], _spec(), plan=PLAN, files=_files(_script()), node=ADD82)
    whys = [o["why"] for o in out]
    assert len(out) == 2, whys
    assert any("nothing has put this host's key on guest 106" in w and "sshpass -e ssh-copy-id" in w for w in whys)
    assert any("VM 106 is stopped" in w and "nothing in this block starts it" in w
               and "qm status 106 | grep -q running || qm start 106" in w for w in whys)
    assert out[0]["command"].startswith("ssh -o BatchMode=yes"), "the refusal names the line"


@pytest.mark.asyncio
async def test_started_and_keyed_the_same_script_passes():
    fixed = _script().replace("# Wait for the VM to be up",
                              "qm status 106 | grep -q running || qm start 106\n# Wait for the VM to be up")
    fixed = fixed.replace("# Install the agent over SSH",
                          'SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new "$PALWORLD_USER@$IP"\n# Install the agent over SSH')
    with patch("app.modules.mcp_client.call_tool", new=_host(qm=QM_LIST_106_STOPPED)):
        assert await pc.unmet([RUN], _spec(), plan=PLAN, files=_files(fixed), node=ADD82) == []


@pytest.mark.asyncio
async def test_sshpass_on_the_ssh_itself_needs_no_key():
    fixed = _script().replace("# Wait for the VM to be up",
                              "qm status 106 | grep -q running || qm start 106\n# Wait for the VM to be up")
    fixed = fixed.replace('ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$PALWORLD_USER@$IP"',
                          'SSHPASS="$MASS_PASSWORD" sshpass -e ssh -o StrictHostKeyChecking=accept-new "$PALWORLD_USER@$IP"')
    with patch("app.modules.mcp_client.call_tool", new=_host(qm=QM_LIST_106_STOPPED)):
        assert await pc.unmet([RUN], _spec(), plan=PLAN, files=_files(fixed), node=ADD82) == []


@pytest.mark.asyncio
async def test_a_guest_a_finished_step_keyed_is_not_refused_for_a_key():
    """ADD26 put the key on the AI VM (110, `ai-vm`): the plan names it by name, qm list by id."""
    node = {"node_key": "ADD49", "title": "Make aiserver (VM 110) reachable on SSH port 22", "description": ""}
    with patch("app.modules.mcp_client.call_tool", new=_host(qm=QM_LIST.replace("110 ai-vm                stopped", "110 ai-vm                running"))):
        out = await pc.unmet(["ssh -o BatchMode=yes aedefruscio@192.168.1.129 'sudo systemctl status ssh'"],
                             _spec(), plan=PLAN, node=node)
    assert out == [], [o["why"][:80] for o in out]
    assert pc.key_known_for("110", "ai-vm", PLAN) and pc.key_known_for("106", "palworld-server", PLAN) is None


@pytest.mark.asyncio
async def test_a_running_guest_is_not_refused_for_being_stopped():
    with patch("app.modules.mcp_client.call_tool", new=_host()):      # 106 running in QM_LIST
        out = await pc.unmet([RUN], _spec(), plan=PLAN, files=_files(_script()), node=ADD82)
    assert len(out) == 1 and out[0]["why"].startswith("nothing has put this host's key on guest 106")


@pytest.mark.asyncio
async def test_the_inventory_is_reused_when_handed_in():
    calls = []
    async def counting(spec, tool, args):
        calls.append(args["command"]); return await _host(qm=QM_LIST_106_STOPPED)(spec, tool, args)
    with patch("app.modules.mcp_client.call_tool", new=counting):
        inv = await pc.read_inventory(_spec())
        assert inv["names"]["106"] == "palworld-server" and inv["vms"]["106"] == "stopped"
        n = len(calls)
        await pc.unmet([RUN], _spec(), plan=PLAN, files=_files(_script()), node=ADD82, inventory=inv)
        assert len(calls) == n, "no second read of the host"


@pytest.mark.asyncio
async def test_an_unreadable_host_still_refuses_nothing_about_a_stopped_guest():
    with patch("app.modules.mcp_client.call_tool", new=_host(pct="", qm="")):
        out = await pc.unmet([RUN], _spec(), plan=PLAN, files=_files(_script()), node=ADD82)
    assert all("is stopped" not in o["why"] for o in out), "a blocker is never invented out of blindness"


@pytest.mark.asyncio
async def test_the_live_third_draft_of_1927_is_refused_for_the_stopped_vm_alone():
    """Trace 1142 (2026-10-02 19:28:00Z): every shape rule satisfied -- sweep,
    MAC → neigh, sshpass ssh-copy-id, sudo on stdin, `<PALWORLD_USER>` -- and no
    `qm start 106` while qm list says stopped. One refusal; with the guarded
    start line, none."""
    from app.modules import supervised_runs as sr
    rb = _script("add82_runbook_1142.md")
    cmds, files = sr.runbook_commands(rb), sr.file_writes(rb)
    assert len(cmds) == 2 and len(files) == 1
    with patch("app.modules.mcp_client.call_tool", new=_host(qm=QM_LIST_106_STOPPED)):
        out = await pc.unmet(cmds, _spec(), plan=PLAN, files=files, node=ADD82)
    assert len(out) == 1 and "VM 106 is stopped" in out[0]["why"], [o["why"][:80] for o in out]
    started = [{**files[0], "content": "qm status 106 | grep -q running || qm start 106\n" + files[0]["content"]}]
    with patch("app.modules.mcp_client.call_tool", new=_host(qm=QM_LIST_106_STOPPED)):
        assert await pc.unmet(cmds, _spec(), plan=PLAN, files=started, node=ADD82) == []


@pytest.mark.asyncio
async def test_a_start_through_a_variable_counts():
    """§17.1288j — trace 1145: `VMID=106` … `qm start "$VMID"` starts 106."""
    from app.modules import supervised_runs as sr
    rb = _script("add82_runbook_1145.md")
    cmds, files = sr.runbook_commands(rb), sr.file_writes(rb)
    with patch("app.modules.mcp_client.call_tool", new=_host(qm=QM_LIST_106_STOPPED)):
        out = await pc.unmet(cmds, _spec(), plan=PLAN, files=files, node=ADD82)
    assert out == [], [o["why"][:100] for o in out]
    assert pc._resolve_ids('VMID=106\nqm start "$VMID"\nqm status ${VMID} | grep -q running') == \
        'VMID=106\nqm start 106\nqm status 106 | grep -q running'
    assert pc._resolve_ids('X=5\nqm start "$X"') == 'X=5\nqm start "$X"', "a non-id value is left alone"


# ───── §17.1288p — a VM whose disk has never been written has no OS to log into

LVS = _script("pve_lvs_2026_10_02.txt")
PLAN_OS = PLAN + [{"node_key": "ADD5", "title": "Install Ubuntu Server 22.04 on VM 106", "status": "done"},
                  {"node_key": "ADD53", "title": "Set VM 106's scsi0 disk to 40G on local-lvm", "status": "done"}]


def _host_full(pct=PCT_LIST, qm=QM_LIST, lvs=LVS, isos="ubuntu-22.04.3-live-server-amd64.iso\nubuntu-26.04-live-server-amd64.iso\n"):
    async def fake(spec, tool, args):
        r = MagicMock(); r.structured = None; r.is_error = False
        c = args["command"]
        r.text = {"pct list": pct, "qm list": qm}.get(c, "")
        if c.startswith("lvs"):
            r.text = lvs
        if c.startswith("ls /var/lib/vz/template/iso"):
            r.text = isos
        return r
    return fake


def test_lvs_parses_thin_and_thick_volumes():
    d = pc.parse_lvs(LVS)
    assert d["106"] == [{"name": "vm-106-disk-0", "data_percent": 0.0}]
    assert d["101"][0]["data_percent"] == 20.01 and d["100"][0]["data_percent"] is None
    assert "data" not in d and "root" not in d


@pytest.mark.asyncio
async def test_the_script_that_ran_twice_is_refused_for_the_empty_disk():
    from app.modules import supervised_runs as sr
    files = [{"path": "/tmp/install_agent_106.sh", "content": _script("add82_install_agent_106_v4.sh")}]
    with patch("app.modules.mcp_client.call_tool", new=_host_full()):
        out = await pc.unmet([RUN], _spec(), plan=PLAN_OS, files=files, node=ADD82)
    whys = [o["why"] for o in out]
    hit = next((w for w in whys if "has never been written" in w), "")
    assert hit, whys
    assert "vm-106-disk-0" in hit and "ADD5" in hit and "ubuntu-22.04.3-live-server-amd64.iso" in hit
    assert "Ubuntu must be installed on VM 106 before this step" in hit


@pytest.mark.asyncio
async def test_a_written_disk_or_a_thick_one_is_not_refused():
    node110 = {"node_key": "ADD49", "title": "Make aiserver (VM 110) reachable on SSH port 22", "description": ""}
    with patch("app.modules.mcp_client.call_tool", new=_host_full(qm=QM_LIST.replace("110 ai-vm                stopped", "110 ai-vm                running"))):
        out = await pc.unmet(["ssh -o BatchMode=yes aedefruscio@192.168.1.129 true"], _spec(), plan=PLAN_OS, node=node110)
    assert all("has never been written" not in o["why"] for o in out), [o["why"][:80] for o in out]
    with patch("app.modules.mcp_client.call_tool", new=_host_full()):
        out = await pc.unmet(["qm status 106 | grep -q running || qm start 106"], _spec(), plan=PLAN_OS, node=ADD82)
    assert all("has never been written" not in o["why"] for o in out), "a start needs no OS"


@pytest.mark.asyncio
async def test_the_inventory_carries_disks_and_isos():
    with patch("app.modules.mcp_client.call_tool", new=_host_full()):
        inv = await pc.read_inventory(_spec())
    assert inv["disks"]["106"][0]["data_percent"] == 0.0 and inv["isos"][0].startswith("ubuntu-22.04.3")


def test_the_empty_disk_fact_is_recorded_for_the_plan():
    from app.modules import execution_agent as ea
    from app.modules import supervised_runs as sr
    src = pathlib_mod.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    # §17.1289 — the fact is one row of machine_truth.contradictions, recorded by reconcile_from_truth
    assert "machine_truth.read_guest_truth(spec, _gid, _inv, _plan_rows)" in body
    assert "machine_truth.reconcile_from_truth(job_id, run_node, _truth, _needs, _plan_rows)" in body
    assert "has never been written" in sr._SHAPE_REFUSALS, "the redraft is told, and the chain counts it"


@pytest.mark.asyncio
async def test_an_if_guard_in_a_script_counts_like_the_or_guard_on_a_line():
    """§17.1294 — the engine's own watch_guest_boot template was refused for its
    `else qm start "$GID"` on a running VM: the guard was an if/else, not `||`."""
    from app.modules import runbook_templates as rt, supervised_runs as sr
    node = {"node_key": "ADD119", "title": "Reset VM 106 and capture its serial console during boot", "description": "boot log"}
    import app.modules.machine_truth as mt
    rb = rt.render(rt.WATCH_GUEST_BOOT, rt.values_for(rt.WATCH_GUEST_BOOT, node, mt.GuestTruth(gid="106", kind="vm", status="running"), {}))
    cmds, files = sr.runbook_commands(rb), sr.file_writes(rb)
    with patch("app.modules.mcp_client.call_tool", new=_host()):          # 106 running in QM_LIST
        out = await pc.unmet(cmds, _spec(), plan=[], files=files, node=node)
    assert all("ALREADY" not in o["why"] for o in out), [o["why"][:100] for o in out]
    assert pc._guarded('GID=106\nif qm status "$GID" | grep -q running; then qm reset "$GID"; else qm start "$GID"; fi\n'.replace('"$GID"', '106'), "qm", "start", "106")
    assert not pc._guarded("qm start 106", "qm", "start", "106")
