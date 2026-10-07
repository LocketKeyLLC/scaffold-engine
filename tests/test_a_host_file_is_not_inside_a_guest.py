"""§17.1403 — a block's files are written on the HOST, host tools exist only there, and a template is not the config.

Live, 2026-10-06. ADD122's draft, the first after §17.1402 told the drafter where
Palworld lives (`service_guests … found={'palworld': '106', 'control-panel': '111'}`):

* it wrote `/tmp/add_palworld_settings.sh` and ran `pct exec 111 -- bash
  /tmp/add_palworld_settings.sh` — the runner writes files on the Proxmox host, so
  inside container 111 that path does not exist;
* the script called `qm guest exec 106 …` — from inside container 111, where there
  is no `qm`;
* it edited `/opt/palworld/DefaultPalWorldSettings.ini`, because §17.1402's facts
  named that file "config". It is the template the server copies FROM; the live file
  is `/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini` (1 byte: the
  server runs on defaults). That fact was mine.
"""
from __future__ import annotations

import inspect
import json
import pathlib

import pytest

from app.modules import service_truth as st
from app.modules import supervised_runs as sr
from app.modules.runbook_templates import TEMPLATES
from app.modules.supervised_runs import a_host_file_run_inside_a_guest as GATE

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add122_host_file_run_in_guest_2026_10_06.json").read_text())


# ── a host file run inside a guest ───────────────────────────────────────────

def test_the_live_draft_is_refused_for_both_reasons():
    out = GATE(LIVE["commands"], LIVE["files"])
    whys = " ".join(r["why"] for r in out)
    assert "inside guest 111 that path does not exist" in whys
    assert "calls `qm`, a Proxmox HOST tool" in whys
    assert "pct push 111 /tmp/add_palworld_settings.sh" in whys


def test_pushed_first_is_fine():
    files = [{"path": "/tmp/x.sh", "content": "echo hi\n"}]
    cmds = ["pct push 111 /tmp/x.sh /tmp/x.sh", "pct exec 111 -- bash /tmp/x.sh"]
    assert GATE(cmds, files) == []


def test_run_on_the_host_is_fine():
    files = [{"path": "/tmp/x.sh", "content": "pct exec 111 -- systemctl restart app\n"}]
    assert GATE(["bash /tmp/x.sh"], files) == []


def test_a_vm_too():
    files = [{"path": "/tmp/x.sh", "content": "echo hi\n"}]
    out = GATE(["qm guest exec 106 -- bash /tmp/x.sh"], files)
    assert out and "inside guest 106" in out[0]["why"]


@pytest.mark.parametrize("cmd", [
    "pct exec 111 -- sh -c 'qm guest exec 106 -- true'",
    "pct exec 111 -- pvesh get /nodes",
])
def test_a_host_tool_in_an_inline_guest_payload(cmd):
    out = GATE([cmd], [])
    assert out and "Proxmox HOST tool" in out[0]["why"]


@pytest.mark.parametrize("cmd", [
    "pct exec 111 -- systemctl restart control-panel.service",
    "pct exec 111 -- ssh -o BatchMode=yes u@192.168.1.106 true",
    "qm guest exec 106 -- cat /etc/hostname",
    "pct exec 111 -- grep -n qmail /etc/services",          # `qmail`, not `qm`
])
def test_what_it_must_not_claim(cmd):
    assert GATE([cmd], []) == [], cmd


def test_the_engines_own_templates_pass():
    for t in TEMPLATES:
        for path, content in (getattr(t, "files", None) or {}).items():
            cmds = [c for c in (getattr(t, "commands", None) or [])] or [f"bash {path}"]
            assert GATE([str(c) for c in cmds], [{"path": path, "content": content}]) == [], t.name


def test_wired_registered_and_the_drafters_gap():
    assert "a_host_file_run_inside_a_guest(cmds, shape_files)" in inspect.getsource(sr.frame_run)
    refused = GATE(LIVE["commands"], LIVE["files"])
    assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP["the engine writes a block's files on the Proxmox"] == "drafter"
    assert sr._WHOSE_GAP["a Proxmox HOST tool"] == "drafter"


# ── a template is not the config ─────────────────────────────────────────────

@pytest.mark.parametrize("path,tpl", [
    ("/opt/palworld/DefaultPalWorldSettings.ini", True),
    ("/etc/app/app.conf.example", True),
    ("/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini", False),
    ("/var/lib/radarr/config.xml", False),
])
def test_what_a_template_is(path, tpl):
    assert st._is_template(path) is tpl


def test_a_template_is_said_plainly_when_it_is_all_there_is():
    s = st.ServiceTruth(guest="106", name="palworld", configs=("/opt/palworld/DefaultPalWorldSettings.ini",))
    says = s.says()
    assert "settings TEMPLATE" in says and "does not read it" in says


def test_the_saved_config_dir_is_a_candidate():
    assert "/opt/palworld/*/Saved/Config/*/*.ini" in st.config_candidates("palworld", "steam", "", workdir="/opt/palworld")


@pytest.mark.asyncio
async def test_the_live_file_leads_and_the_engines_inis_are_dropped(monkeypatch):
    """The measured shape of VM 106: the live file is 1 byte, the template has the keys,
    and the saved-config dir holds forty engine .ini files."""
    show = ("Id=palworld.service\nUser=steam\nGroup=steam\nActiveState=active\nLoadState=loaded\n"
            "WorkingDirectory=/opt/palworld\nExecStart={ path=/opt/palworld/PalServer.sh ; argv[]=/opt/palworld/PalServer.sh ; }\n")
    live = "/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
    tpl = "/opt/palworld/DefaultPalWorldSettings.ini"
    engine = "/opt/palworld/Pal/Saved/Config/LinuxServer/Engine.ini"

    async def probe(spec, c):
        if "ss -tlnp" in c:
            return True, ""
        if "systemctl show" in c:
            return True, show
        if "ls -1d" in c:
            return True, f"{tpl}\n{engine}\n{live}\n"
        if "grep -c" in c:
            return True, f"{tpl}:120\n{live}:0\n"
        if "stat -c" in c:
            return True, f"steam:steam 644 {tpl}\nsteam:steam 644 {live}\n"
        return True, ""
    monkeypatch.setattr(st, "_probe", probe)
    s = (await st.read_services(object(), "106", mentioned=["palworld"], vm=True))[0]
    assert s.configs[0] == live
    assert engine not in s.configs
    assert s.configs[-1] == tpl
