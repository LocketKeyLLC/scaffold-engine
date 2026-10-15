"""§17.1603 — a block never puts a secret into its own output; a Proxmox host command never runs in a guest.

Live, 2026-10-15 (c2792d4b T14, "Create a read-only Proxmox API token for the panel"): the draft was wrapped in the
run-in-container template (so `pveum`, a host-only tool, would have run inside CT 111) and ran
`pveum user token add … | tee /root/panel-api-token.txt` with the check `cat /root/panel-api-token.txt` -- the
token's one-time secret printed into the record twice. Lines below are the live frame's.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import runbook_templates as rt
from app.modules.supervised_runs import prints_a_secret

LIVE_REMOTE = ('pveum user add panel-api@pve --comment "Panel read-only API user" 2>/dev/null; pveum acl modify / '
               '--users panel-api@pve --roles PVEAuditor; pveum user token add panel-api@pve panel-token --privsep 0 '
               '--expire 0 2>/dev/null | tee /root/panel-api-token.txt')
LIVE_CHECK = 'pct exec 111 -- bash -c "cat /root/panel-api-token.txt"'


def test_bites_on_the_live_tee_and_the_live_check():
    out = prints_a_secret([], [LIVE_CHECK], [{"path": "/tmp/in_ct_111.sh", "content": LIVE_REMOTE}])
    assert len(out) == 2
    assert "`tee` copies it to the screen" in " ".join(o["why"] for o in out)
    assert any("/root/panel-api-token.txt" in o["why"] and "Prove it WORKS" in o["why"] for o in out)


@pytest.mark.parametrize("line", [
    "pveum user token add panel@pve panel --privsep 0 --output-format json > /root/t.json",
    "pveum user token add panel@pve panel --output-format json | python3 -c 'import json,sys; ...' > /root/t.json",
    "PW=$(pwgen 24 1)",
    "stat -c '%a %U' /root/t.json",
    "cat /etc/pve/pve-root-ca.pem",
    "cat /opt/panel/config/network-view.json",
])
def test_the_safe_shapes_pass(line):
    assert prints_a_secret([line]) == [], line


@pytest.mark.parametrize("line", [
    "pveum user token add panel@pve panel",
    "openssl rand -hex 32",
    "cat /opt/panel/config/secrets.json",
    "head -n 3 /root/api_key.txt",
    "cat /etc/ssl/private/site.key",
])
def test_the_printing_shapes_are_refused(line):
    assert prints_a_secret([line]) != [], line


def test_a_proxmox_host_admin_step_gets_no_in_guest_template():
    node = json.loads((pathlib.Path(__file__).parent / "fixtures" / "panel_t14_node_2026_10_15.json").read_text())
    assert rt._HOST_ADMIN_RE.search(node["prompt_template"])
    truth = type("T", (), {"gid": "111", "status": "running", "kind": "ct"})()
    t = rt.select_template(node, truth)
    assert t not in (rt.RUN_IN_CONTAINER, rt.RUN_IN_VM_VIA_AGENT, rt.REACH_VM_SSH_AND_RUN)


def test_the_gate_is_wired_into_frame_run():
    import inspect
    from app.modules import supervised_runs
    assert "prints_a_secret(cmds, verify, shape_files)" in inspect.getsource(supervised_runs.frame_run)


# §17.1603b — the host's admin work for a guest is not "changing the host instead of the guest"
def test_the_live_host_side_token_draft_is_not_refused_as_missing_the_guest():
    from app.modules.supervised_runs import commands_never_reach_the_guest
    node = json.loads((pathlib.Path(__file__).parent / "fixtures" / "panel_t14_node_2026_10_15.json").read_text())
    cmds = ["pveum user list --output-format json | grep -q '\"userid\": \"panel@pve\"' || pveum user add panel@pve",
            "pveum acl modify / --roles PVEAuditor --users panel@pve",
            "umask 077; pveum user token add panel@pve panel --privsep 0 --output-format json > /tmp/panel-token.json",
            "pct push 111 /tmp/panel-token.json /opt/panel/config/proxmox-token.json",
            "rm -f /tmp/panel-token.json"]
    assert commands_never_reach_the_guest(cmds, node) == []
    assert prints_a_secret(cmds) == []
    other = {"title": "Install the agent in VM 106", "description": "apt-get install qemu-guest-agent"}
    assert commands_never_reach_the_guest(["apt-get install -y qemu-guest-agent"], other) != [], "the rule still bites"


# §17.1603c — the develop flow never shows the model a secret file's value, and never writes one
def test_a_secret_file_reaches_the_model_as_its_shape_only():
    from app.modules import develop as dv
    body = '{"full-tokenid": "panel@pve!panel", "info": {"privsep": "0"}, "value": "real-secret-value"}'
    shown = dv.mask_secret_file("/opt/panel/config/proxmox-token.json", body)
    assert "real-secret-value" not in shown and "panel@pve!panel" not in shown
    assert json.loads(shown) == {"full-tokenid": dv.SECRET_MASK, "info": {"privsep": dv.SECRET_MASK}, "value": dv.SECRET_MASK}
    assert dv.mask_secret_file("/opt/panel/config/api-key.txt", "abc") == dv.SECRET_MASK + "\n"
    assert dv.mask_secret_file("/opt/panel/config/network-view.json", '{"a": 1}') == '{"a": 1}', "others untouched"


def test_a_version_that_writes_a_secret_file_is_refused():
    from app.modules import develop as dv
    assert dv.writes_a_secret_file({"/opt/panel/config/proxmox-token.json": "{}"}) != []
    assert dv.writes_a_secret_file({"/opt/panel/network_ui_route.js": "x", "/opt/panel/config/media-request.json": "{}"}) == []
    import inspect
    from app.modules import execution_agent
    assert "develop.writes_a_secret_file(files)" in inspect.getsource(execution_agent)


# §17.1608 — reads are not ended by a failed read
def test_a_reads_only_guest_block_is_not_set_e_and_a_writing_one_is():
    live_t2 = "free -h\ndf -h / /var/lib/docker\nnvidia-smi\nlspci | grep -i nvidia\nlsblk\ncat /proc/meminfo | head -5"
    assert rt.strict_line(live_t2).startswith("set +e")
    assert rt.strict_line("apt-get install -y docker.io\nsystemctl enable --now docker") == "set -e"
    assert rt.strict_line("") == "set -e", "a template's own work stays strict"
    for t in (rt.RUN_IN_CONTAINER, rt.RUN_IN_VM_VIA_AGENT, rt.REACH_VM_SSH_AND_RUN):
        body = rt.render(t, {"GID": "110", "STEP": "T2", "REMOTE_COMMANDS": live_t2, "GUEST_USER_NAME": "AI_USER"})
        assert "{STRICT}" not in body and "set +e" in body, t.name
