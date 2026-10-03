"""§17.1290 — templates the drafter fills: every template passes every gate,
selection follows the measured guest, the model fills only the free parameter."""
from __future__ import annotations

import pathlib

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

POLICY = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
ENV = {"profile": "root@pve", "system_state": {"host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}},
                                                "106": {"kind": "vm", "attrs": {"name": "palworld-server"}},
                                                "111": {"kind": "ct", "attrs": {"hostname": "control-panel"}}}}
ADD117 = {"node_key": "ADD117", "title": "Install Ubuntu 22.04 on VM 106 unattended (cloud image + cloud-init)",
          "description": "VM 106 (palworld-server) has NO operating system … install Ubuntu Server 22.04 unattended."}
ADD82 = {"node_key": "ADD82", "title": "Install and enable QEMU Guest Agent in VM 106",
         "description": "Install qemu-guest-agent inside the palworld-server guest (VM 106) and confirm it answers."}
ADD100 = {"node_key": "ADD100", "title": "Rebuild the control panel to do what was chosen in ADD99",
          "description": "Rework the control-panel backend and frontend in LXC 111 so it does the things chosen."}
ADD94 = {"node_key": "ADD94", "title": "Re-attach VM 106's detached disk and grow it to 100G", "description": "qm set / qm resize on the host."}
REMOTE = "apt-get update\napt-get install -y qemu-guest-agent\nsystemctl enable --now qemu-guest-agent"


def _truth(kind, agent=False, key=None):
    return mt.GuestTruth(gid="106" if kind == "vm" else "111", kind=kind, status="running", agent=agent, key_known_by=key)


def _frame(runbook, node):
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    return sr.frame_run(node, runbook, spec, POLICY, env=ENV)


def test_every_template_passes_every_gate():
    cases = [
        (rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), {}),
        (rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm"), {"REMOTE_COMMANDS": REMOTE}),
        (rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm", key="ADD26 · Install the SSH public key"), {"REMOTE_COMMANDS": REMOTE}),
        (rt.RUN_IN_CONTAINER, ADD100, _truth("ct"), {"REMOTE_COMMANDS": "apt-get update\napt-get install -y nodejs"}),
    ]
    for tpl, node, truth, model_vals in cases:
        rb = rt.render(tpl, rt.values_for(tpl, node, truth, ENV, model_vals))
        assert rt.template_of(rb) == tpl.name
        frame = _frame(rb, node)
        assert frame["refused"] == [], (tpl.name, [r["why"][:120] for r in frame["refused"]])
        assert frame["commands"] and frame["files"] and "run" in {o["id"] for o in frame["options"]}
        assert any("drafted from the engine's template" in w for w in frame["engine_fixed"]), tpl.name
        if tpl is rt.RUN_IN_CONTAINER:
            assert frame["inputs"] == []
        else:
            assert [i["name"] for i in frame["inputs"]] == ["PALWORLD_USER"], tpl.name
            assert frame["inputs"][0]["suggestions"] == [], "the host shell's root is not offered for the guest"


def test_the_install_template_carries_the_days_lessons():
    rb = rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), ENV))
    body = sr.file_writes(rb)[0]["content"]
    assert 'qm set "$GID" --ide2 local-lvm:cloudinit --ciuser "$USER_NAME" --cipassword "$MASS_PASSWORD"' in body
    assert "--sshkeys \"$PUBKEY\" --ipconfig0 ip=dhcp" in body and "qm importdisk" in body
    assert 'DISK_SIZE="100G"' in body and 'qm resize "$GID" scsi0 "$DISK_SIZE"' in body
    assert body.index("nmap -sn") < body.index("ip neigh show"), "the sweep comes before the read"
    assert "| head -n 1 || true)" in body, "every lookup a wait expects to be empty ends in || true"
    assert "set -uo pipefail" in body and "set -e" not in body
    assert sr.runbook_commands(rb) == ['MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<PALWORLD_USER>" bash /tmp/install_os_106.sh']
    assert sr.verify_commands(rb) == ["qm config 106 | grep -E '^(scsi0|ide2|boot):'", "qm status 106"]


def test_the_reach_template_copies_the_key_only_when_none_is_known():
    yes = rt.render(rt.REACH_VM_SSH_AND_RUN, rt.values_for(rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm"), ENV, {"REMOTE_COMMANDS": REMOTE}))
    no = rt.render(rt.REACH_VM_SSH_AND_RUN, rt.values_for(rt.REACH_VM_SSH_AND_RUN, ADD82, _truth("vm", key="ADD26"), ENV, {"REMOTE_COMMANDS": REMOTE}))
    assert 'if [ "yes" = "yes" ]' in yes and 'if [ "no" = "yes" ]' in no
    assert "sshpass -e ssh-copy-id" in yes and "sudo -S -p '' bash -s" in yes and REMOTE in yes


def test_selection_follows_the_step_and_the_measured_guest():
    assert rt.select_template(ADD117, _truth("vm")) is rt.INSTALL_OS_CLOUDINIT
    assert rt.select_template(ADD82, _truth("vm")) is rt.REACH_VM_SSH_AND_RUN
    assert rt.select_template(ADD82, _truth("vm", agent=True)) is None, "with the agent up, today's path (qm guest exec) is fine"
    assert rt.select_template(ADD100, _truth("ct")) is rt.RUN_IN_CONTAINER
    assert rt.select_template(ADD94, _truth("vm")) is None, "host-side work on a guest is not a guest template"
    assert rt.select_template({"node_key": "X", "title": "Install the NVIDIA driver on the Proxmox host", "description": ""}, None) is None
    assert rt.select_template(ADD82, None) is None, "an unmeasured guest selects nothing"


def test_the_model_fills_one_fence_and_nothing_else():
    assert rt.model_fence("Here:\n```bash\n$ apt-get update\n# comment\napt-get install -y x\n```\nthanks") == "apt-get update\napt-get install -y x"
    assert rt.model_fence("apt-get update") == "apt-get update"


def test_the_drafter_renders_a_template_first_and_the_pause_measures_before_drafting():
    src = pathlib.Path(sr.__file__).read_text(encoding="utf-8")
    i = src.index("async def draft_runbook(")
    assert "truth=None" in src[i:i + 400] and "rt.select_template(node, truth)" in src[i:] and "rt.render(tpl" in src[i:]
    assert src.index("rt.select_template(node, truth)", i) < src.index("prompt = build_base_prompt(node, b, environment)", i)
    from app.modules import execution_agent as ea
    esrc = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    j = esrc.index("async def _pause_for_decision(")
    ebody = esrc[j:esrc.index("\nasync def ", j + 10)]
    assert ebody.index("machine_truth.read_guest_truth(") < ebody.index("spec=spec, environment=_env, truth=_truth)")


# ───── §17.1290b — the first live pass: the import, and the OS-install step is not blocked by the empty disk

import pytest


@pytest.mark.asyncio
async def test_a_template_without_a_free_parameter_asks_the_model_nothing():
    assert await rt.fill_free_params(rt.INSTALL_OS_CLOUDINIT, ADD117, "brief") == {}


def test_the_free_parameter_draw_imports_the_router_from_where_it_lives():
    src = pathlib.Path(rt.__file__).read_text(encoding="utf-8")
    assert "from app import model_router" in src and "from app.modules import model_router" not in src
    i = src.index("async def fill_free_params(")
    body = src[i:]
    assert body.index("return {}") < body.index("from app import model_router"), "no import before the early return"


@pytest.mark.asyncio
async def test_the_install_template_is_not_refused_for_the_disk_it_exists_to_fill():
    """§17.1288p blocks a step that reaches into a VM with a never-written disk;
    the OS-install step is the one exception, and its script sshes in AFTER."""
    from unittest.mock import MagicMock, patch
    from app.modules import runbook_preconditions as pc
    rb = rt.render(rt.INSTALL_OS_CLOUDINIT, rt.values_for(rt.INSTALL_OS_CLOUDINIT, ADD117, _truth("vm"), ENV))
    cmds, files = sr.runbook_commands(rb), sr.file_writes(rb)
    lvs = (pathlib.Path(__file__).parent / "fixtures" / "pve_lvs_2026_10_02.txt").read_text(encoding="utf-8")
    qm = "      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID\n       106 palworld-server      running    8192               0.00 1\n"

    async def fake(spec, tool, args):
        r = MagicMock(); r.structured = None; r.is_error = False
        c = args["command"]; r.text = {"qm list": qm, "pct list": ""}.get(c, "")
        if c.startswith("lvs"): r.text = lvs
        return r
    spec = MagicMock(); spec.name = "pve-runner"
    plan = [{"node_key": "ADD5", "title": "Install Ubuntu Server 22.04 on VM 106", "status": "done"}]
    with patch("app.modules.mcp_client.call_tool", new=fake):
        out = await pc.unmet(cmds, spec, plan=plan, files=files, node=ADD117)
    assert all("has never been written" not in o["why"] for o in out), [o["why"][:100] for o in out]
    assert out == [], [o["why"][:100] for o in out]
