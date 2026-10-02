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
