"""§17.1327 — a systemd unit a block acts on must be one the engine holds, and the
detached phases print the unit's log once.

Live, 2026-10-03 19:30 UTC: the reopened ADD66 "Start VM 106 and bring the PalWorld
service up on UDP 8211" drafted on the model path with `systemctl is-enabled
palworld-server` / `is-active palworld-server` — the VM's hostname. No such unit
exists; `palworld.service` sat in T23's and T24's records. Every gate was green.
And T23's eight wait phases each re-printed the whole SteamCMD tail."""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

FX = pathlib.Path(__file__).parent / "fixtures"
ADD66_MODEL = json.loads((FX / "add66_frame_model_2026_10_03.json").read_text(encoding="utf-8"))
NODE66 = {"node_key": "ADD66", "title": "Start VM 106 and bring the PalWorld service up on UDP 8211",
          "description": "Start VM 106 (palworld-server) on the Proxmox host, then confirm inside the guest that the "
                         "PalWorld service is enabled, active, and listening on UDP 8211."}
#: the engine's own records of the unit it wrote (T23/T24), as the pause hands them over
HELD = "T23: installed app 2394010 under /opt/palworld; unit palworld.service enabled (not started)"
ENV = {"profile": "root@pve", "facts": ["VM 106 (palworld-server) runs the PalWorld dedicated server"],
       "system_state": {"106": {"kind": "vm", "attrs": {"name": "palworld-server"}}}}
POLICY = {"allow": ["ANY"], "sudo": True, "helper": "19", "secrets": ["MASS_PASSWORD"], "can_write_files": True}


def _frame(rb, node=NODE66, env=ENV, upstream=""):
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    return sr.frame_run(node, rb, spec, POLICY, env=env, upstream=upstream)


def test_the_live_model_draft_is_refused_and_the_real_unit_is_named():
    cmds = ADD66_MODEL["commands"]
    assert any("is-enabled palworld-server" in c for c in cmds), "the live draft read the hostname as a unit"
    out = sr.unsourced_service_name(cmds, [], ENV, NODE66, upstream=HELD)
    assert len(out) == 1, [r["why"][:100] for r in out]
    assert "`palworld-server` is not a unit" in out[0]["why"]
    assert "`palworld.service` — use that" in out[0]["why"], out[0]["why"]
    assert sr.shape_retry_note({"refused": out, "commands": cmds}), "registered: the chain redrafts"


def test_the_guests_name_in_the_system_map_is_not_a_unit():
    """The map holds `palworld-server` as the VM's name; that is why the check asks
    for the name AS A UNIT rather than as a word."""
    assert json.dumps(ENV).count("palworld-server") >= 1
    assert sr.unsourced_service_name(["systemctl is-active palworld-server"], [], ENV, NODE66)


def test_the_machine_settles_it_and_names_the_unit_it_has():
    """§17.1327 — the guest's own `systemctl list-unit-files`. Live: CT 111 has
    `control-panel.service` (117 units), VM 106 has `palworld.service` (201) and no
    `palworld-server`. A unit the machine has needs no other source; a unit it does
    not have is refused even when a word like it is in view."""
    vm_units = ["palworld", "ssh", "qemu-guest-agent", "systemd-resolved"]
    ct_units = ["control-panel", "cron", "ssh"]
    assert sr.unsourced_service_name(["systemctl enable --now palworld.service"], [], ENV, NODE66, units=vm_units) == []
    # the legitimate restart a step in view never mentions: the machine has it
    panel = {"node_key": "ADD100", "title": "Rebuild the control panel in LXC 111", "description": ""}
    assert sr.unsourced_service_name(["systemctl restart control-panel"], [], {"profile": "x"}, panel, units=ct_units) == []
    assert sr.unsourced_service_name(["systemctl restart control-panel"], [], {"profile": "x"}, panel, units=vm_units)
    hit = sr.unsourced_service_name(["systemctl is-enabled palworld-server"], [], ENV, NODE66, units=vm_units)
    assert len(hit) == 1 and "the guest's own `systemctl list-unit-files` does not have it" in hit[0]["why"]
    assert "The unit the guest has is `palworld.service`" in hit[0]["why"], hit[0]["why"]


def test_the_truth_reads_the_units_off_the_guest():
    """The pure half, on the live listing's shape."""
    listing = ("apt-daily.service                      static          -\n"
               "autovt@.service                        alias           -\n"
               "palworld.service                       enabled         enabled\n"
               "ssh.service                            enabled         enabled\n")
    truth = mt.truth_from_texts("106", inventory={"cts": {}, "vms": {"106": "running"}, "names": {}, "disks": {}},
                                plan=[], units_text=listing)
    assert truth.units == ["apt-daily", "autovt@", "palworld", "ssh"]
    assert truth.reads["systemctl list-unit-files"] == "4 units"
    blind = mt.truth_from_texts("106", inventory={"cts": {}, "vms": {"106": "running"}, "names": {}, "disks": {}}, plan=[])
    assert blind.units is None, "unread is unknown, not empty"


def test_a_unit_the_engine_holds_or_the_block_writes_passes():
    # the corrected ADD66: the step's own text names the unit, the block only starts it
    node = {**NODE66, "description": "the unit is /etc/systemd/system/palworld.service (written by T23). "
                                     "Start it: systemctl enable --now palworld.service"}
    assert sr.unsourced_service_name(["systemctl enable --now palworld.service", "systemctl is-active palworld.service"],
                                     [], ENV, node) == []
    # T23's shape: the block writes the unit file, then enables it
    body = ("cat > /etc/systemd/system/palworld.service <<EOF\n[Unit]\nEOF\nsystemctl daemon-reload\n"
            "systemctl enable palworld.service\n")
    assert sr.unsourced_service_name(["bash /tmp/x.sh"], [{"path": "/tmp/x.sh", "content": body}], ENV, NODE66) == []
    # an earlier step's output is a source
    assert sr.unsourced_service_name(["systemctl is-active palworld.service"], [], ENV, NODE66, upstream=HELD) == []


def test_stock_units_variables_and_non_acting_verbs_are_left_alone():
    for cmd in ("systemctl restart ssh", "systemctl is-active qemu-guest-agent", "systemctl enable docker",
                "systemctl restart systemd-resolved", "systemctl is-active caddy", "systemctl daemon-reload",
                'systemctl is-active "$UNIT"', "systemctl show -p Result --value $UNIT", "systemctl reset-failed $UNIT"):
        assert sr.unsourced_service_name([cmd], [], ENV, NODE66) == [], cmd


def test_every_frame_in_the_pause_carries_the_measured_units():
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert body.count("supervised_runs.frame_run(") >= 8
    # §17.1357 — the units are computed ONCE (the subject guest's list plus the
    # units measured on the step's other guests, by guest) and every frame carries
    # both. Eight copies of the expression is how sibling call sites drift.
    assert body.count("units=_units, units_by_guest=_units_by_guest") == body.count("supervised_runs.frame_run(")
    assert "units=(_truth.units if _truth is not None else None)" not in body


def test_the_engines_own_templates_name_only_units_they_write():
    """A gate that refuses the engine's own blocks is a template bug; the Steam
    template writes `{GAME}.service` before it enables it."""
    T23 = {"node_key": "T23", "title": "Install PalWorld server", "description": ""}
    vals = rt.values_for(rt.INSTALL_STEAM_SERVER, T23, mt.GuestTruth(gid="106", kind="vm", status="running", agent=True), ENV, {})
    rb = rt.render(rt.INSTALL_STEAM_SERVER, vals)
    frame = _frame(rb, T23)
    assert frame["refused"] == [], [r["why"][:120] for r in frame["refused"]]
    for tpl, node, truth, mv in ((rt.RUN_IN_VM_VIA_AGENT, NODE66, mt.GuestTruth(gid="106", kind="vm", status="running", agent=True),
                                  {"REMOTE_COMMANDS": "systemctl enable --now palworld.service", "VERIFY_INSIDE": "systemctl is-active palworld.service"}),):
        rb2 = rt.render(tpl, rt.values_for(tpl, node, truth, ENV, mv))
        node2 = {**node, "description": node["description"] + " unit: palworld.service"}
        assert _frame(rb2, node2)["refused"] == []


# ───── the detached phases print the unit's log once

def test_the_phase_script_prints_the_log_once(tmp_path):
    rb = rt.render(rt.RUN_IN_VM_VIA_AGENT,
                   rt.values_for(rt.RUN_IN_VM_VIA_AGENT, {"node_key": "T23", "title": "Install PalWorld server", "description": ""},
                                 mt.GuestTruth(gid="106", kind="vm", status="running", agent=True), ENV,
                                 {"REMOTE_COMMANDS": "true", "VERIFY_INSIDE": "true"}))
    body = next(f["content"] for f in sr.file_writes(rb))
    assert ".scaffold_step.reported" in body and "printed by the phase that saw" in body
    assert "/root/.scaffold_step.reported;" in body, "a rerun clears the sentinel with the logs"
    assert subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True).returncode == 0
    # drive the real wait phase twice against a stub `qm`: the guest's files live in tmp_path
    stub = tmp_path / "qm"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# a stub `qm`: answers systemd's queries the way the agent does, and runs anything else in the guest dir\n"
        "cmd=\"${!#}\"\n"
        "case \"$cmd\" in\n"
        "  *'systemctl is-active'*) out='inactive' ;;\n"
        "  *'-p Result'*)           out='success' ;;\n"
        "  *'-p ExecMainStatus'*)   out='0' ;;\n"
        "  *)                       out=\"$(cd %s && bash -c \"$cmd\" 2>/dev/null)\" ;;\n"
        "esac\n"
        "python3 -c 'import json,sys; print(json.dumps({\"exitcode\": 0, \"exited\": 1, \"out-data\": sys.argv[1] + chr(10)}))' \"$out\"\n"
        % tmp_path)
    stub.chmod(0o755)
    guest = tmp_path / "root"; guest.mkdir()
    (guest / ".scaffold_step.log").write_text("Success! App '2394010' fully installed.\n")
    (guest / ".scaffold_step.err").write_text("")
    script = tmp_path / "phase.sh"; script.write_text(body.replace("/root/.scaffold_step", str(guest / ".scaffold_step")))
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)}
    first = subprocess.run(["bash", str(script), "wait"], capture_output=True, text=True, env=env)
    second = subprocess.run(["bash", str(script), "wait"], capture_output=True, text=True, env=env)
    assert "fully installed" in first.stdout, first.stdout[:300] + first.stderr[:300]
    assert "fully installed" not in second.stdout, second.stdout[:300]
    assert "printed by the phase that saw" in second.stdout
    assert first.returncode == 0 and second.returncode == 0
