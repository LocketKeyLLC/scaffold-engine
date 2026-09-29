"""§17.1199 — "anything I approve" as an explicit trust level.

The enumerated allow-list can only be discovered by FAILING: every step reveals
a command nobody predicted, and each one costs the operator a console paste and
a re-install on the target machine. On the live 23-step plan that is longer
than doing the build by hand — the operator's words were "You are putting a
bandaid on a gushing wound".

And it was redundant. The operator already approves every block: they read the
exact commands and press a button. The list was a second gate enforcing the
same decision at one round-trip per discovery.

`ANY` moves the trust boundary to the MACHINE and the APPROVAL, which is what
Ansible's `become`, Jenkins agents and self-hosted runners all do. What still
holds: the catastrophic denylist, the per-block approval, the per-command HMAC,
the single-use nonce, the TTL, and the fact that nothing runs unless the
operator pressed the button for that exact block.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

from app.modules import assist_supervised as sw

ROOT = pathlib.Path(__file__).resolve().parents[1]
ANY_LIST = [sw.ANY]

HOST_POWER = ["reboot", "shutdown -h now", "systemctl poweroff", "halt"]


def _runner():
    spec = importlib.util.spec_from_file_location("local_runner_mcp", ROOT / "scripts" / "local_runner_mcp.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


@pytest.mark.parametrize("cmd", [
    "qm agent 106 ping",
    "apt-get install -y nginx",
    "printf 'x' | tee /etc/caddy/Caddyfile",
    "for i in 1 2 3; do echo $i; done",
    "cp /etc/network/interfaces /etc/network/interfaces.bak.$(date +%s)",
    "echo hello > /tmp/somefile",
])
def test_any_lets_an_approved_command_through_whatever_its_shape(cmd):
    """The shape rules exist to make PREFIX MATCHING sound — a `$(…)` can hide
    a command from a prefix. With no prefix to match they buy nothing, and they
    were why a Caddyfile step could never run at all."""
    assert sw.write_allowed(cmd, ANY_LIST) == (True, "")


@pytest.mark.parametrize("cmd", HOST_POWER)
def test_any_does_not_lift_the_catastrophic_denylist(cmd):
    """`write_allowed` is only half the gate: `gate_block` (engine) and
    `run_supervised` (runner) both check `catastrophic` FIRST, so nothing the
    operator can switch on reaches past it."""
    runnable, refused = sw.gate_block([cmd], ANY_LIST)
    assert runnable == [] and len(refused) == 1
    assert refused[0]["why"] == sw.catastrophic(cmd) != ""


def test_any_is_still_a_channel_that_can_be_off():
    assert sw.write_allowed("", ANY_LIST)[0] is False
    assert sw.write_allowed("qm set 1 --x", [])[0] is False      # no list is still no channel


def test_the_narrow_list_is_completely_unchanged():
    assert sw.write_allowed("qm start 106", ["qm set"])[0] is False       # a write, not listed
    assert sw.write_allowed("qm set 106 --scsi0 x", ["qm set"]) == (True, "")
    assert sw.write_allowed("echo x > /tmp/f", ["echo"])[1].startswith("redirect")


def test_a_read_passes_the_write_gate_and_that_is_the_whole_add65_story():
    """`qm agent 106 ping` is READ-ONLY, so it passes the write gate whatever
    the list says — and then, in narrow mode, runs UNPRIVILEGED. That is why
    `qm start 106` succeeded as root and the very next command in the same
    approved block came back `ipcc_send_rec … Unable to load access control
    list`. The write list was never the thing standing in its way."""
    assert sw.write_allowed("qm agent 106 ping", ["qm set"]) == (True, "")
    r = _runner()
    assert r.apply_sudo_policy("sudo qm agent 106 ping", ["qm set"])[0] == "qm agent 106 ping"   # no grant
    assert r.apply_sudo_policy("qm agent 106 ping", [r.ANY])[0] == "sudo -n qm agent 106 ping"   # ANY: root


def test_both_ends_agree_about_any():
    r = _runner()
    assert r.ANY == sw.ANY
    for cmd in ("qm agent 106 ping", "apt-get install -y nginx", "printf 'x' | tee /etc/f"):
        assert r.write_allowed(cmd, [r.ANY]) == sw.write_allowed(cmd, ANY_LIST)
    for cmd in HOST_POWER:
        assert r.catastrophic(cmd) == sw.catastrophic(cmd) != ""


def test_any_grants_root_for_reads_too():
    """The write/read split is what made `qm start 106` succeed as root and
    `qm agent 106 ping` fail unprivileged two commands later, inside ONE
    approved block. Under ANY both get the grant."""
    r = _runner()
    text_, unresolved = r.sudoers_writes_text([r.ANY], user="scaffold-runner")
    assert "scaffold-runner ALL=(root) NOPASSWD: ALL" in text_ and unresolved == []
    assert r.apply_sudo_policy("qm status 106", [r.ANY])[0] == "sudo -n qm status 106"
    # the narrow mode still refuses to elevate what is not listed
    assert r.apply_sudo_policy("sudo qm status 106", ["qm config"])[0] == "qm status 106"


def test_the_supervised_path_elevates_reads_only_under_any():
    src = (ROOT / "scripts" / "local_runner_mcp.py").read_text(encoding="utf-8")
    run_sup = src[src.index("async def run_supervised"):src.index("    return mcp")]
    assert "if write_sudo and (ANY in writes or not read_only(command)[0]):" in run_sup
