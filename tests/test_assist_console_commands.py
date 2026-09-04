"""§17.922-924 — the operator types console commands by hand.

Operator: "have it give the CORRECT command lines as well as reckonize that
within the console copy and paste is not possible. as well as it not giving
sudo within the console commands."

The sudo half was a REGRESSION from §17.913, which decided host-vs-guest by
matching prose in the preceding 400 characters and missed six of eight real
phrasings — so `sudo` was stripped from GUEST commands, which then fail
permission-denied.
"""
from __future__ import annotations

import pytest

from app.modules.assist_guide import (
    _block_runs_on_root_host,
    _IN_GUEST_MARKER_RE,
    repair_console_commands,
    repair_unavailable_tools,
)

pytestmark = pytest.mark.asyncio

ROOT_ENV = {"profile": "Operator runs commands as root@pve in ONE interactive shell",
            "missing_tools": [{"tool": "sudo", "host": "root@pve"}]}


# ── §17.922 classification is by the 📍 banner, not prose ────────────────

def test_host_banner_marks_a_block_as_root_host():
    t = "📍 On: the Proxmox host shell (root@pve)\n```bash\nqm stop 106\n```"
    assert _block_runs_on_root_host(t, t.index("```bash"))


def test_guest_banner_marks_a_block_as_guest():
    t = "📍 In: the Proxmox VM 106 Console (noVNC)\n```bash\napt update\n```"
    assert not _block_runs_on_root_host(t, t.index("```bash"))


def test_prose_after_a_host_banner_overrides_it():
    """A walkthrough banners the host once and then moves into the guest in
    prose. Without this the guest block inherits the host banner and loses its
    sudo — which is permission-denied for the operator."""
    t = ("📍 On: the Proxmox host shell (root@pve)\n```bash\nqm stop 106\n```\n"
         "Now run these inside the VM Console:\n```bash\nsudo apt update\n```")
    second = t.rindex("```bash")
    assert not _block_runs_on_root_host(t, second)


def test_unbannered_block_is_never_stripped():
    """The 👉 headline block precedes its own banner. When nothing identifies
    the machine, leave the command alone: a needless sudo is obvious and
    recoverable, a missing one is neither."""
    t = "```bash\napt update\n```"
    assert not _block_runs_on_root_host(t, 0)


@pytest.mark.parametrize("prose", [
    # every one observed in a real live walkthrough during §17.922-924 testing
    "Log into the VM via the Proxmox Web UI Console and type this:",
    "Open the Proxmox Web UI Console for VM 106 and type this:",
    "Type these commands one by one in the VM Console:",
    "Run these commands in the VM:",
    "From the VM shell:",
    "On the Ubuntu server:",
    "At the palworld-server login prompt, run:",
    "Once logged into Ubuntu, run:",
    "Now run these inside the VM Console:",
])
def test_guest_phrasings_are_recognised(prose):
    assert _IN_GUEST_MARKER_RE.search(prose)


@pytest.mark.parametrize("prose", [
    "Run this now:", "On the Proxmox host shell (root@pve)",
    "From the Proxmox host, run:", "root@pve:~# qm config 106",
])
def test_host_phrasings_are_not_mistaken_for_a_guest(prose):
    assert not _IN_GUEST_MARKER_RE.search(prose)


def test_guest_sudo_survives_and_host_sudo_is_dropped():
    t = ("📍 On: the Proxmox host shell (root@pve)\n```bash\nsudo qm stop 106\n```\n"
         "Now run these inside the VM Console:\n"
         "```bash\nsudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv\n```")
    out, _ = repair_unavailable_tools(t, ROOT_ENV)
    assert "qm stop 106" in out and "sudo qm stop" not in out
    assert "sudo lvextend" in out


# ── §17.924 console commands are typed, so they must not be chained ──────

def test_console_chains_are_split_host_chains_are_not():
    """Live: the body of a fix correctly listed the commands one per line AND
    said "no copy-paste in this window", while its own headline emitted
    `apt update && apt install -y qemu-guest-agent`."""
    t = ("📍 In: the Proxmox Web UI Console (VM 106)\n"
         "```bash\napt update && apt install -y qemu-guest-agent\n```\n"
         "📍 On: the Proxmox host shell (root@pve)\n"
         "```bash\nqm stop 106 && qm set 106 --boot order=scsi0\n```")
    out, notes = repair_console_commands(t)
    assert "apt update\napt install -y qemu-guest-agent" in out      # split
    assert "qm stop 106 && qm set 106 --boot order=scsi0" in out     # untouched
    assert any("split" in n for n in notes)


def test_privileged_console_commands_missing_sudo_are_reported():
    t = ("📍 In: the VM Console\n```bash\napt install -y qemu-guest-agent\n```")
    _out, notes = repair_console_commands(t)
    assert any("need `sudo`" in n for n in notes)


def test_comments_and_blank_lines_survive_the_split():
    t = "📍 In: the VM Console\n```bash\n# check first\nip a\n```"
    out, _ = repair_console_commands(t)
    assert "# check first" in out and "ip a" in out


def test_console_rule_is_shared_by_guide_and_fix_prompts():
    """It was added to the guide prompt only, and the very next live fix emitted
    an 88-character double-chained apt command for a console."""
    import inspect
    from app.modules import assist_guide
    src = inspect.getsource(assist_guide)
    assert src.count("parts.append(_CONSOLE_TYPING_RULE)") == 2
    assert "NO copy-paste" in assist_guide._CONSOLE_TYPING_RULE
    assert "still needs sudo" in assist_guide._CONSOLE_TYPING_RULE or \
           "need `sudo`" in assist_guide._CONSOLE_TYPING_RULE or \
           "needs `sudo`" in assist_guide._CONSOLE_TYPING_RULE
