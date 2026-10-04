"""§17.1342 — the read goes where the command goes.

§17.1332 taught the engine to read an *arr API key off the machine instead of
asking for it, and substituted the read inline on the HOST:

    curl -H "X-Api-Key: $(pct exec 103 -- sh -c 'cat …' | sed …)" http://192.168.1.22:7878/…

Measured on pve-runner, 2026-10-04, that cannot work. The runner raises its own
privileges for the LEADING command only, so a `pct` inside `$( … )` runs as the
runner's own unprivileged user and fails:

    ipcc_send_rec[1] failed: Unknown error -1
    Unable to load access control list: Unknown error -1

The key then comes back EMPTY, the request goes out with an empty header, and the
app answers 401 — which reads like a wrong key, not like a read that never
happened. The shape that works reads the key inside the guest that owns it, in
the same command that calls the API there:

    pct exec 103 -- sh -c 'curl -s -H "X-Api-Key: $(cat … | sed -n "…" | head -n 1)" \
        http://127.0.0.1:7878/api/v3/rootfolder'

Live, that returns Radarr's real answer.
"""
from __future__ import annotations

import subprocess

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr

INV = {"names": {"102": "prowlarr", "103": "radarr", "104": "sonarr", "111": "control-panel"}}
IN_GUEST = ("pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: <RADARR_API_KEY>\" "
            "http://127.0.0.1:7878/api/v3/rootfolder'")
FROM_HOST = 'curl -s -H "X-Api-Key: <RADARR_API_KEY>" http://192.168.1.22:7878/api/v3/rootfolder'


def test_the_in_guest_read_carries_no_pct():
    r = mv.readable_for("RADARR_API_KEY")
    inside = r.read(None)
    assert "pct" not in inside and "qm" not in inside
    assert "/var/lib/radarr/config.xml" in inside


def test_the_sed_script_is_double_quoted_so_it_survives_sh_minus_c():
    """The read is substituted inside `sh -c '…'`, whose quotes are single."""
    inside = mv.readable_for("RADARR_API_KEY").read(None)
    assert "sed -n \"" in inside, inside
    assert "sed -n '" not in inside, inside


def test_a_command_that_runs_in_the_guest_gets_the_read():
    cmds, _v, notes = mv.read_on_the_machine([IN_GUEST], [], INV)
    assert "<RADARR_API_KEY>" not in cmds[0]
    assert "pct exec 103 -- sh -c 'curl" in cmds[0]
    assert cmds[0].count("pct exec") == 1, "the read adds no second pct"
    assert len(notes) == 1 and "inside guest 103" in notes[0]["why"]


def test_a_host_command_is_left_alone_and_said_so():
    """The host form cannot work, so it is not written. The value stays an input
    and the gate hands the drafter the shape that does work."""
    cmds, _v, notes = mv.read_on_the_machine([FROM_HOST], [], INV)
    assert cmds == [FROM_HOST] and notes == []
    out = mv.still_asked([{"name": "RADARR_API_KEY", "secret": True}], INV)
    assert len(out) == 1
    why = out[0]["why"]
    assert "pct exec 103 -- sh -c" in why and "127.0.0.1" in why
    assert "unprivileged" in why and "401" in why


def test_addresses_guest_reads_both_ways_in():
    assert mv.addresses_guest("pct exec 103 -- sh -c 'x'", "103") is True
    assert mv.addresses_guest("qm guest exec 106 -- sh -c 'x'", "106") is True
    assert mv.addresses_guest("pct exec 104 -- sh -c 'x'", "103") is False
    assert mv.addresses_guest("curl http://192.168.1.22:7878", "103") is False
    assert mv.addresses_guest("", "103") is False


def test_the_rendered_command_is_valid_shell():
    """Three levels of quoting — `sh -c '…'`, a double-quoted header, a
    double-quoted sed script inside a substitution. Bash settles it."""
    cmds, _v, _n = mv.read_on_the_machine([IN_GUEST], [], INV)
    p = subprocess.run(["bash", "-n"], input="#!/bin/bash\n" + cmds[0] + "\n", text=True, capture_output=True)
    assert p.returncode == 0, p.stderr


def test_the_checks_are_filled_the_same_way():
    _c, verify, notes = mv.read_on_the_machine(["pct exec 103 -- true"], [IN_GUEST], INV)
    assert "<RADARR_API_KEY>" not in verify[0] and len(notes) == 1


RUNBOOK_IN_GUEST = """## Run this

```bash
pct exec 103 -- sh -c 'curl -s -H "X-Api-Key: <RADARR_API_KEY>" http://127.0.0.1:7878/api/v3/rootfolder'
```

## Verify

- Radarr's root folders: `pct exec 103 -- sh -c 'curl -s -H "X-Api-Key: <RADARR_API_KEY>" http://127.0.0.1:7878/api/v3/rootfolder'`
"""


def test_the_frame_asks_for_nothing_when_the_call_runs_in_the_guest():
    spec = type("S", (), {"name": "pve-runner", "headers": {}, "endpoint": "http://192.168.1.156:8790/mcp/"})()
    policy = {"allow": ["ANY"], "sudo": True, "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    node = {"node_key": "ADD131", "title": "Set Radarr's root folder to /media/movies",
            "description": "Radarr runs in container 103 on port 7878."}
    frame = sr.frame_run(node, RUNBOOK_IN_GUEST, spec, policy, env={"profile": "root@pve"}, inventory=INV)
    assert [i["name"] for i in frame["inputs"]] == [], frame["inputs"]
    assert "/var/lib/radarr/config.xml" in " ".join(frame["commands"] + frame["verify"])
    assert not any("API_KEY" in str(r.get("command", "")) for r in frame["refused"]), frame["refused"]
