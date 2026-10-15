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
