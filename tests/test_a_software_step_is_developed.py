"""§17.1412 — a step that writes software is developed against the real files, tested, and
delivered by an engine-owned template; it is not drafted as one blind shell block.

ADD122 went through ~20 drafts and ~20 engine fixes (PRs #768–#787), each draft failing on a
new defect. The operator: "you are fixing a large number of small issues instead of
addressing all of the issues as a whole." The shared causes: the drafter never saw the files
it changed; it wrote the feature AND its delivery in one shell block; each attempt was a
fresh guess. These tests pin the redesign's parts.
"""
from __future__ import annotations

import base64
import inspect
import json
import pathlib
import re
import subprocess

import pytest

from app.modules import develop as dv
from app.modules import execution_agent
from app.modules import service_truth as st
from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD122 = json.loads((FIX / "add122_node_2026_10_07.json").read_text())
PANEL = st.ServiceTruth(guest="111", name="control-panel", unit="control-panel.service", user="",
                        ports=("3001",), workdir="/opt/control-panel-backend")
PAL = st.ServiceTruth(guest="106", vm=True, name="palworld", unit="palworld.service", user="steam",
                      workdir="/opt/palworld")
HOST = dv.host_for(ADD122, [PANEL, PAL])
FILES = {"/opt/control-panel-backend/server.js": "const express = require('express');\nconst app = express();\n"
                                                 "app.use(express.json());\napp.listen(3001);\n",
         "/opt/control-panel-backend/routes/palworld-settings.js": "module.exports = (app) => {};\n"}


# ── which steps are developed ───────────────────────────────────────────────

def test_the_live_add122_is_developed_on_the_panel():
    assert HOST is not None
    assert (HOST.guest, HOST.workdir, HOST.unit, HOST.vm) == ("111", "/opt/control-panel-backend", "control-panel.service", False)


@pytest.mark.parametrize("node,services", [
    ({"title": "Set onboot on VM 110", "description": "qm set 110 --onboot 1"}, [PANEL]),         # builds nothing
    ({"title": "LXC 111: implement the X capability", "description": "a route"}, [st.ServiceTruth(guest="111", name="x", unit="x.service")]),  # no directory
])
def test_a_step_that_is_not_software_in_a_service_directory_is_not(node, services):
    assert dv.host_for(node, services) is None


# ── what the model may write ─────────────────────────────────────────────────

@pytest.mark.parametrize("path,ok", [
    ("/opt/control-panel-backend/server.js", True),
    ("/opt/control-panel-backend/routes/new.js", True),
    ("/etc/systemd/system/control-panel.service", True),
    ("/etc/passwd", False),
    ("/opt/control-panel-backend/../../etc/shadow", False),
    ("relative/server.js", False),
    ("/opt/control-panel-backend/", False),
    ("/root/.ssh/authorized_keys", False),
])
def test_paths(path, ok):
    assert dv.allowed_path(path, HOST, ADD122) is ok


def test_a_directory_the_step_names_is_allowed():
    node = {"title": "LXC 111: build the frontend", "description": "create /opt/control-panel-ui with a page"}
    assert dv.allowed_path("/opt/control-panel-ui/index.html", HOST, node)
    assert not dv.allowed_path("/opt/other/index.html", HOST, node)


# ── the delivery is the engine's shape, and the engine's own parsers and gates accept it ──

def _staged(path: str) -> str:
    """§17.1413c — the flat staged name: the runner's write_file creates no directory."""
    return "/tmp/scaffold-dev-ADD122--" + path.strip("/").replace("/", "--")


def test_every_staged_file_sits_directly_in_tmp():
    """Live: a nested stage (/tmp/scaffold-dev/ADD122/opt/...) was refused by the runner --
    "'…' is not a directory on this machine" -- before anything ran."""
    for f in sr.file_writes(_delivery()):
        assert f["path"].rsplit("/", 1)[0] == "/tmp", f["path"]


def _delivery(files=FILES, host=HOST):
    return dv.render_delivery(ADD122, host, files, dv.done_checks(ADD122, host))


def test_the_steps_own_checks_are_the_verify():
    checks = dv.done_checks(ADD122, HOST)
    assert any("curl -s http://127.0.0.1:3001/api/palworld-settings" in c for c in checks)
    assert checks[-1] == "pct exec 111 -- systemctl is-active control-panel.service"


def test_the_runbook_parses_into_files_one_command_and_the_checks():
    rb = _delivery()
    files = {f["path"]: f["content"] for f in sr.file_writes(rb)}
    for p, c in FILES.items():
        assert files[_staged(p)].strip() == c.strip(), p
    assert "/tmp/scaffold-dev-ADD122--deliver.sh" in files
    assert sr.runbook_commands(rb) == ["bash /tmp/scaffold-dev-ADD122--deliver.sh"]
    assert sr.verify_commands(rb) == dv.done_checks(ADD122, HOST)


def test_the_delivery_script_is_valid_bash_and_does_the_whole_job():
    sh = {f["path"]: f["content"] for f in sr.file_writes(_delivery())}["/tmp/scaffold-dev-ADD122--deliver.sh"]
    assert subprocess.run(["bash", "-n"], input=sh, text=True).returncode == 0
    assert "tar czf \"$BACKUP\"" in sh, "backs up the directory first"
    for p in FILES:
        assert f"pct push 111 {_staged(p)} {p}" in sh
    assert "pct exec 111 -- systemctl restart control-panel.service" in sh
    assert "systemctl is-active control-panel.service" in sh


def test_the_delivery_passes_the_engines_own_shape_gates():
    rb = _delivery()
    files, cmds = sr.file_writes(rb), sr.runbook_commands(rb)
    assert sr.a_host_file_run_inside_a_guest(cmds, files) == []
    assert sr.variables_nothing_sets(cmds, files, {}) == []
    assert sr.a_pipe_the_guest_agent_never_reads(cmds, files) == []
    assert sr.the_agents_json_read_as_text(cmds, files) == []
    assert sr.json_used_as_shell_quoting(cmds, files) == []


def test_a_vm_service_is_delivered_through_its_agent_with_stdin():
    vm = dv.Host(guest="106", vm=True, workdir="/opt/app", unit="app.service", user="steam", group="steam")
    sh = {f["path"]: f["content"] for f in sr.file_writes(dv.render_delivery(ADD122, vm, {"/opt/app/a.js": "x"}, ["true"]))}
    sh = sh["/tmp/scaffold-dev-ADD122--deliver.sh"]
    assert "qm guest exec 106 --pass-stdin 1 -- sh -c" in sh and "chown steam:steam /opt/app/a.js" in sh
    assert subprocess.run(["bash", "-n"], input=sh, text=True).returncode == 0
    assert sr.a_pipe_the_guest_agent_never_reads(["bash /tmp/scaffold-dev-ADD122--deliver.sh"],
                                                 [{"path": "/tmp/scaffold-dev-ADD122--deliver.sh", "content": sh}]) == []


def test_a_file_that_would_break_the_runbook_is_carried_base64_and_round_trips():
    tricky = "line one\n```\n## not a heading\n### nor this\n"
    rb = _delivery({"/opt/control-panel-backend/README.md": tricky})
    files = {f["path"]: f["content"] for f in sr.file_writes(rb)}
    assert _staged("/opt/control-panel-backend/README.md") not in files
    sh = files["/tmp/scaffold-dev-ADD122--deliver.sh"]
    b64 = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", sh).group(1)
    assert base64.b64decode(b64).decode() == tricky
    assert "carried inside deliver.sh base64-encoded" in rb


# ── the model is shown the real files and its own last version ──────────────

def test_the_prompt_carries_the_current_files_the_previous_version_and_the_evidence():
    p = dv.build_prompt(ADD122, HOST, "SERVICES MEASURED …", {"/opt/control-panel-backend/server.js": "OLD SERVER"},
                        {"/opt/control-panel-backend/server.js": "MY VERSION"}, "- PUT 400", 2)
    assert "OLD SERVER" in p and "MY VERSION" in p and "- PUT 400" in p
    assert "unit control-panel.service" in p and "runs as root" in p


def test_scoring_puts_a_clean_frame_first_and_non_rehearsal_refusals_last():
    from app.modules import rehearsal as rh
    clean = {"commands": ["x"], "refused": []}
    rehearsal_only = {"commands": ["x"], "refused": [{"why": rh.MARK + " … PUT 400"}]}
    gate = {"commands": ["x"], "refused": [{"why": "a shape gate"}]}
    report = {"commands": [], "roundtrip": {"ok": False, "get_status": 200, "put_status": 400}}
    assert dv.score(clean, None) == 0 < dv.score(rehearsal_only, report) < dv.score(gate, None)


# ── wired: the shell chain never touches a developed frame ──────────────────

def test_the_pause_develops_instead_of_drafting_and_the_chain_stands_aside():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert 'runbook = "" if _developed else await supervised_runs.draft_runbook(' in src
    assert "frame = await _develop_the_step(_dev_host, frame)" in src
    assert "preconditions=await _pre_for(rb)" in src and "develop.render_delivery(" in src
    for guard in ("if fix and not _developed:", "_rehearsal_only(frame) and not _developed:",
                  'if not frame.get("commands") and not _developed:', 'or _developed) else supervised_runs.verify_not_runnable',
                  '_ro = "" if _developed else', "_missing = [] if _developed else"):
        assert guard in src, guard


# ── §17.1413 — engine-owned building blocks ──────────────────────────────────

NODE_UNIT = "[Service]\nWorkingDirectory=/opt/control-panel-backend\nExecStart=/usr/bin/node server.js\n"
WS = {"/etc/systemd/system/control-panel.service": NODE_UNIT, "/opt/control-panel-backend/server.js": "x"}


def test_a_node_service_gets_the_kit_at_its_own_directory():
    kit = dv.kit_for(HOST, WS)
    assert sorted(kit) == ["/opt/control-panel-backend/scaffold-kit/remote.js",
                           "/opt/control-panel-backend/scaffold-kit/ue_settings.js"]
    assert "sudo', 'tee'" in kit["/opt/control-panel-backend/scaffold-kit/remote.js"]
    assert "OptionSettings=(" in kit["/opt/control-panel-backend/scaffold-kit/ue_settings.js"]


def test_a_service_that_is_not_node_gets_no_kit_yet():
    assert dv.kit_for(HOST, {"/etc/systemd/system/control-panel.service": "ExecStart=/usr/bin/python3 app.py"}) == {}


def test_the_prompt_documents_the_kit_at_its_real_path():
    doc = dv.kit_doc(HOST, dv.kit_for(HOST, WS))
    assert "require('/opt/control-panel-backend/scaffold-kit/remote')" in doc
    assert "Never build an ssh command line yourself" in doc
    p = dv.build_prompt(ADD122, HOST, "facts", WS, None, "", 1, doc)
    assert "THE ENGINE'S KIT" in p


def test_the_kit_is_delivered_with_every_version_and_the_model_cannot_overwrite_it():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "files = {p: c for p, c in files.items() if p not in kit}" in src
    assert "develop.render_delivery(run_node, host, {**files, **kit}, checks)" in src
    rb = dv.render_delivery(ADD122, HOST, {**FILES, **dv.kit_for(HOST, WS)}, ["true"])
    staged = {f["path"] for f in sr.file_writes(rb)}
    assert _staged("/opt/control-panel-backend/scaffold-kit/remote.js") in staged


def test_every_round_logs_its_evidence():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "develop.evidence_of(fr)[:400]" in src


def test_the_rehearsal_gets_the_measured_units_and_users():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "known_units=[" in src and 'users=[{"user": s.user, "uid": s.uid' in src


# ── §17.1413b — the acceptance test is stated, and the exchange is always shown ──

TARGET = {"get": "http://127.0.0.1:3001/api/palworld-settings", "put": "http://127.0.0.1:3001/api/palworld-settings",
          "config": "/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini",
          "baseline": "/opt/palworld/DefaultPalWorldSettings.ini"}


def test_the_test_is_stated_up_front():
    """Live: six rounds stalled on GET `{"settings": {...}}` vs a PUT that took the body as the map
    itself -- the model was never told the test is GET, then PUT exactly that body."""
    t = dv.test_contract(TARGET)
    assert "GET http://127.0.0.1:3001/api/palworld-settings" in t
    assert "with body B EXACTLY" in t and "must accept exactly the shape the GET returns" in t
    assert "DefaultPalWorldSettings.ini" in t
    assert dv.test_contract(None) == ""
    assert "THE TEST THE ENGINE RUNS" in dv.build_prompt(ADD122, HOST, "f", {}, None, "", 1, "", t)


def test_the_pause_hands_the_test_to_every_round():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "develop.test_contract(_rh_target)" in src


def test_a_put_that_succeeded_but_mangled_the_file_shows_the_exchange():
    from app.modules import rehearsal as rh
    rep = {"commands": [], "roundtrip": {
        "ok": False, "get_status": 200, "get_body": '{"settings":{"Difficulty":"None"}}',
        "put_status": 200, "put_body": '{"ok":true}', "header_kept": True, "expected_keys": 122, "got_keys": 1,
        "extra": ["settings"], "file_after": "[/Script/Pal.PalGameWorldSettings]\nOptionSettings=(settings=[object Object])"}}
    why = rh.refusal_from(rep)[0]["why"]
    assert 'what the test sent: GET answered `{"settings":{"Difficulty":"None"}}`' in why
    assert "the PUT answered 200" in why
    assert "settings=[object Object]" in why
