"""§17.1402 — the engine measures a service in a VM, not only in a container.

Live, 2026-10-06. ADD122 (a control-panel backend that edits the Palworld server's
settings) was drafted against `/home/aedefruscio/Steam/…/PalServer` and an ssh as
`aedefruscio`. Measured on VM 106 through the read-only box: the install is
`/opt/palworld`, `palworld.service` runs as `User=steam` with
`WorkingDirectory=/opt/palworld`, the directory is `drwxr-xr-x steam` — and the
engine had told the drafter none of it:

* every read in `read_services` was `pct exec`, which reaches containers only;
* the sweep that places a named service swept containers only;
* it places a service by its LISTENING process name, and Palworld listens as
  `PalServer-Linux-Shipping`;
* `config_candidates` knew only `-data=` dirs and `/var/lib/<name>`.

Proven live after the fix (read-only, through the real runner):
`palworld on guest 106 (at 192.168.1.106) · port 38405 · runs as steam (uid 1001,
gid 1001) · unit palworld.service (active) · installed in /opt/palworld · config
/opt/palworld/DefaultPalWorldSettings.ini`; sweep `{'radarr': '103', 'palworld': '106'}`.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import execution_agent
from app.modules import service_truth as st

#: what VM 106 answered, unwrapped from the agent's JSON (2026-10-06)
SHOW = ("Id=palworld.service\nUser=steam\nGroup=steam\nExecStart={ path=/opt/palworld/PalServer.sh ; "
        "argv[]=/opt/palworld/PalServer.sh ; }\nFragmentPath=/etc/systemd/system/palworld.service\n"
        "ActiveState=active\nLoadState=loaded\nWorkingDirectory=/opt/palworld\n")
SS = ('LISTEN 0 4096 0.0.0.0:38405 0.0.0.0:* users:(("PalServer-Linux",pid=901,fd=40))\n')


def _runner(sent: list):
    async def probe(spec, command):
        sent.append(command)
        if "ss -tlnp" in command:
            return True, SS
        if "hostname -I" in command:
            return True, "192.168.1.106 "
        if "systemctl show" in command and "palworld" in command:
            return True, SHOW
        if "systemctl show" in command:
            return True, "LoadState=not-found\n"
        if command.endswith("'id steam'") or "id steam" in command:
            return True, "uid=1001(steam) gid=1001(steam) groups=1001(steam)"
        if "ls -1d" in command:
            return True, "/opt/palworld/DefaultPalWorldSettings.ini\n"
        return True, ""
    return probe


@pytest.mark.asyncio
async def test_a_vm_service_is_read_through_the_agent(monkeypatch):
    sent: list = []
    monkeypatch.setattr(st, "_probe", _runner(sent))
    out = await st.read_services(object(), "106", mentioned=["palworld"], vm=True)
    assert out and all(c.startswith("qm guest exec 106 --") for c in sent), sent
    s = out[0]
    assert (s.user, s.unit, s.workdir, s.vm) == ("steam", "palworld.service", "/opt/palworld", True)
    assert s.configs[0] == "/opt/palworld/DefaultPalWorldSettings.ini"
    says = s.says()
    assert "runs as steam" in says and "installed in /opt/palworld" in says


@pytest.mark.asyncio
async def test_a_container_is_still_read_with_pct(monkeypatch):
    sent: list = []
    monkeypatch.setattr(st, "_probe", _runner(sent))
    await st.read_services(object(), "111", mentioned=["palworld"])
    assert sent and all(c.startswith("pct exec 111 --") for c in sent)


@pytest.mark.asyncio
async def test_the_sweep_covers_running_vms_and_places_by_guest_name(monkeypatch):
    sent: list = []
    monkeypatch.setattr(st, "_probe", _runner(sent))
    found = await st.guests_of_the_named_services(
        object(), ["palworld"], {"111": "running"}, vms={"106": "running", "100": "stopped"},
        guest_names={"106": "palworld-server", "111": "control-panel", "100": "gpu-vm"})
    assert found == {"palworld": "106"}
    assert any(c.startswith("qm guest exec 106 --") for c in sent)
    assert not any(" 100 " in c for c in sent), "a stopped VM is not probed"


@pytest.mark.asyncio
async def test_a_name_that_matches_two_guests_places_nothing(monkeypatch):
    monkeypatch.setattr(st, "_probe", _runner([]))
    found = await st.guests_of_the_named_services(
        object(), ["palworld"], {}, vms={"106": "running", "107": "running"},
        guest_names={"106": "palworld-a", "107": "palworld-b"})
    assert found == {}


@pytest.mark.asyncio
async def test_a_stopped_guests_name_places_nothing(monkeypatch):
    monkeypatch.setattr(st, "_probe", _runner([]))
    found = await st.guests_of_the_named_services(
        object(), ["palworld"], {}, vms={"106": "stopped"}, guest_names={"106": "palworld-server"})
    assert found == {}


@pytest.mark.parametrize("wd,want", [("/opt/palworld", "/opt/palworld"), ("-/opt/x", "/opt/x"),
                                     ("!/srv/y", "/srv/y"), ("~", ""), ("", "")])
def test_working_directory_modifiers(wd, want):
    got = wd.lstrip("-!+")
    assert (got if got.startswith("/") else "") == want


def test_workdir_is_a_config_candidate():
    c = st.config_candidates("palworld", "steam", "", workdir="/opt/palworld")
    assert "/opt/palworld/*.ini" in c


def test_the_hint_says_the_right_command_for_the_kind():
    vm = st.ServiceTruth(guest="106", vm=True)
    ct = st.ServiceTruth(guest="103")
    assert st.exec_hint(vm).startswith("qm guest exec 106 --")
    assert st.exec_hint(ct).startswith("pct exec 103 --")


def test_the_drafting_path_passes_the_vms_and_names():
    src = inspect.getsource(execution_agent)
    assert "vms=_vms" in src and 'guest_names=(_inv or {}).get("names")' in src
    assert "vm=_g in _vms and _g not in _cts" in src


def test_no_probe_hard_codes_pct_any_more():
    src = inspect.getsource(st)
    assert src.count('f"pct exec {gid} --') == 1, "only in_guest itself writes it"
    assert 'f"pct exec {gid} --' in inspect.getsource(st.in_guest)
