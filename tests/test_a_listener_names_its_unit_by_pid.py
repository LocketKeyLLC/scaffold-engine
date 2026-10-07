"""§17.1410 — a listener's unit is found by its pid, not guessed from its process name.

Live, 2026-10-07. CT 111's control panel listens as `node`; its unit is
`control-panel.service`. `systemctl show node` finds nothing, so the engine knew the
unit only when the step's text happened to say "control-panel" -- and once ADD122's
done-condition was rewritten (operator's "behaviour checks"), it did not. The pause
measured `named=['palworld']`, the drafter was told nothing about the panel's unit, and
every draft guessed `control-panel-backend` (the rehearsal: "Unit control-panel-backend
not found"). `ss -tlnp` carries the pid; `ps -o unit= -p PID` names the owning unit.
"""
from __future__ import annotations

import pytest

from app.modules import service_truth as st

SS = ('State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n'
      'LISTEN 0      511          0.0.0.0:3001      0.0.0.0:*    users:(("node",pid=412,fd=19))\n')
SHOW = ("Id=control-panel.service\nUser=\nGroup=\nActiveState=active\nLoadState=loaded\n"
        "WorkingDirectory=/opt/control-panel-backend\n")


def test_pids_are_read_from_ss():
    assert st.pids_in(SS) == {"node": "412"}


@pytest.mark.asyncio
async def test_the_unit_is_found_by_pid_when_nothing_names_it(monkeypatch):
    sent: list = []

    async def probe(spec, c):
        sent.append(c)
        if "ss -tlnp" in c:
            return True, SS
        if "ps -o unit= -p 412" in c:
            return True, "control-panel.service\n"
        if "systemctl show" in c and "control-panel" in c:
            return True, SHOW
        if "systemctl show" in c:
            return True, "LoadState=not-found\n"
        return True, ""
    monkeypatch.setattr(st, "_probe", probe)
    out = await st.read_services(object(), "111", mentioned=["palworld"])     # the live pause's names
    assert [s.unit for s in out] == ["control-panel.service"]
    assert any("ps -o unit= -p 412" in c for c in sent)


@pytest.mark.asyncio
async def test_a_pid_with_no_unit_falls_back_to_the_process_name(monkeypatch):
    async def probe(spec, c):
        if "ss -tlnp" in c:
            return True, SS
        if "ps -o unit=" in c:
            return True, "-\n"                       # not under systemd
        if "systemctl show" in c and " node" in c:
            return True, SHOW.replace("control-panel.service", "node.service")
        return True, "LoadState=not-found\n"
    monkeypatch.setattr(st, "_probe", probe)
    out = await st.read_services(object(), "111")
    assert [s.unit for s in out] == ["node.service"]
