"""§17.1332 — a value a machine holds is read on the machine, not asked of the operator.

Live, 2026-10-03, the operator on the home-lab job: "The api keys for radarr and
sonarr the engine should be able to retrieve itself, it did so with PRowlarr."

The record shows how both were true at once. ADD115's RUN block read all three
*arr keys off the disk itself (`get_key(103, "/var/lib/radarr/config.xml")`),
while the same step's CHECKS asked the operator to paste them:

    - How many indexers Prowlarr now has (paste the key from the previous check):
      `curl -s -H "X-Api-Key: <PROWLARR_API_KEY>" http://<PROWLARR_IP>:9696/api/v1/indexer`

`verify_needs_a_value_the_run_never_used` (§17.1331) does not bite here: the run
DID use the value, it just never shared it. The fixture below is that Verify
section, verbatim from ADD115's stored output.
"""
from __future__ import annotations

import pathlib
import subprocess

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr

FX = pathlib.Path(__file__).parent / "fixtures"
#: ADD115's own Verify section, as stored on the node
LIVE_CHECKS = (FX / "add115_checks_2026_10_03.txt").read_text(encoding="utf-8")
#: what `pct list` said that pause: the engine names the guest, this file does not
INV = {"names": {"102": "prowlarr", "103": "radarr", "104": "sonarr", "105": "download-client",
                 "101": "jellyfin", "111": "control-panel"}}
POLICY = {"allow": ["ANY"], "sudo": True, "secrets": ["MASS_PASSWORD"], "can_write_files": True}
NODE = {"node_key": "ADD115", "title": "Add indexers to Prowlarr and sync them to Radarr and Sonarr",
        "description": "Prowlarr runs in container 102, Radarr in 103, Sonarr in 104."}


def _checks() -> list[str]:
    """The three live check commands, pulled out of the fixture's backticks."""
    import re
    out = [m for ln in LIVE_CHECKS.split("\n")
           for m in re.findall(r"`([^`]*X-Api-Key[^`]*)`", ln)]
    assert len(out) == 4, out          # prowlarr twice, radarr, sonarr
    return out


def test_the_live_checks_asked_the_operator_for_three_keys():
    """The pre-image: this is what the operator saw."""
    checks = _checks()
    assert all("<" in c and "_API_KEY>" in c for c in checks)
    inputs = sr.inputs_for([], checks, "")
    asked = sorted(i["name"] for i in inputs if i["name"].endswith("API_KEY"))
    assert asked == ["PROWLARR_API_KEY", "PROWLARR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"][1:] or \
        set(asked) == {"PROWLARR_API_KEY", "RADARR_API_KEY", "SONARR_API_KEY"}, asked
    assert all(i["secret"] for i in inputs if i["name"].endswith("API_KEY")), "and they are secrets"


def test_the_engine_reads_each_key_off_its_own_guest():
    cmds, verify, notes = mv.read_on_the_machine([], _checks(), INV)
    assert not any("_API_KEY>" in v for v in verify), verify
    for app, gid in (("prowlarr", "102"), ("radarr", "103"), ("sonarr", "104")):
        hit = [v for v in verify if f"/var/lib/{app}/config.xml" in v]
        assert hit, (app, verify)
        assert f"pct exec {gid} --" in hit[0], hit[0]
        assert "<ApiKey>" in hit[0], "it reads the element the app writes"
    assert len(notes) == 3 and all("instead of asking" in n["why"] for n in notes)


def test_the_substituted_checks_are_valid_shell():
    """A command substitution inside a quoted header, with a sed script inside it:
    bash settles whether that parses, not the author."""
    _, verify, _ = mv.read_on_the_machine([], _checks(), INV)
    script = "#!/bin/bash\n" + "\n".join(verify) + "\n"
    p = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert p.returncode == 0, p.stderr


def test_nothing_is_substituted_when_no_machine_is_named():
    """Blindness invents nothing: with no `lidarr` guest in the inventory the
    placeholder stays, and stays an operator input."""
    check = ['curl -s -H "X-Api-Key: <LIDARR_API_KEY>" http://192.168.1.30:8686/api/v1/system/status']
    _, verify, notes = mv.read_on_the_machine([], check, INV)
    assert verify == check and notes == []
    assert mv.still_asked([{"name": "LIDARR_API_KEY"}], INV) == []


def test_a_value_no_machine_holds_is_left_alone():
    """The operator's password, an address, a name: not this fix's business."""
    cmds = ['sshpass -e ssh aedefruscio@<PALWORLD_IP> true', 'echo "<MASS_PASSWORD>"', 'curl http://<RADARR_IP>:7878']
    out, _, notes = mv.read_on_the_machine(cmds, [], INV)
    assert out == cmds and notes == []
    assert mv.readable_for("MASS_PASSWORD") is None and mv.readable_for("RADARR_IP") is None


def test_the_gate_refuses_a_key_that_reaches_the_frame_as_a_question():
    """The other end (§17.1085): if a draft still ships the value as an input — a
    file's placeholder, a name the substitution did not match — the frame refuses
    with the read in the remedy instead of asking."""
    out = mv.still_asked([{"name": "RADARR_API_KEY", "secret": True}, {"name": "MASS_PASSWORD"}], INV)
    assert len(out) == 1 and out[0]["command"] == "the value <RADARR_API_KEY>"
    assert "/var/lib/radarr/config.xml" in out[0]["why"] and "pct exec 103" in out[0]["why"]


def _frame(runbook, inventory=INV):
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    return sr.frame_run(NODE, runbook, spec, POLICY, env={"profile": "root@pve"},
                        inventory=inventory)


LIVE_RUNBOOK = """## Run this

```bash
pct exec 103 -- systemctl is-active radarr
```

## Verify

- Radarr's root folders: `curl -s -H "X-Api-Key: <RADARR_API_KEY>" http://192.168.1.22:7878/api/v3/rootfolder`
"""


def test_the_frame_asks_for_nothing_and_keeps_the_check():
    """End to end: no input, the check survives (§17.1288d would have DROPPED it as
    a value the run never uses), and the frame says what it read and from where."""
    frame = _frame(LIVE_RUNBOOK)
    assert [i["name"] for i in frame["inputs"]] == [], frame["inputs"]
    assert len(frame["verify"]) == 1, frame["verify"]
    assert "/var/lib/radarr/config.xml" in frame["verify"][0]
    assert any("read RADARR_API_KEY off the machine" in w for w in frame["engine_fixed"]), frame["engine_fixed"]
    assert not any("API_KEY" in str(r.get("command", "")) for r in frame["refused"]), frame["refused"]


def test_without_an_inventory_the_frame_behaves_exactly_as_before():
    """The host unreadable: the value is asked for, nothing is refused on its
    account. A measurement that did not happen changes nothing."""
    frame = _frame(LIVE_RUNBOOK, inventory=None)
    assert any(i["name"] == "RADARR_API_KEY" for i in frame["inputs"]) or frame["verify"] == []
    assert not any("not a question for the operator" in str(r.get("why", "")) for r in frame["refused"])
