"""§17.913 — what the operator's box actually has.

Live (session 613dd1df, turns 1423-1428): the environment profile records
"Operator runs commands as root@pve in ONE interactive shell", and the engine
emitted `sudo lvextend … && sudo resize2fs …`. Proxmox is Debian minimal: no
sudo, and root does not need it. The operator got `-bash: sudo: command not
found` TWICE, four minutes apart, and the fix in between prescribed
`qm config 106` — it never registered the actual error.
"""
from __future__ import annotations

import pytest

from app.modules.assist_guide import (
    find_missing_tools,
    find_unavailable_tools,
    operator_is_root,
    repair_unavailable_tools,
)

pytestmark = pytest.mark.asyncio

LIVE_1424 = ("root@pve:~# sudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv\n"
             "-bash: sudo: command not found")
ROOT_ENV = {"profile": "Operator runs commands as root@pve in ONE interactive shell",
            "missing_tools": [{"tool": "sudo", "host": "root@pve"}]}


def test_detects_the_live_missing_sudo():
    assert find_missing_tools(LIVE_1424) == ["sudo"]


def test_zsh_form_reports_the_tool_not_the_shell():
    """REGRESSION — `zsh: command not found: jq` names the SHELL first and the
    tool last; bash is the reverse. A single alternation matched the bash branch
    on "zsh: command not found", the shell-name filter discarded it, and the
    scan resumed PAST the real tool, so the zsh form reported nothing."""
    assert find_missing_tools("zsh: command not found: jq") == ["jq"]
    assert find_missing_tools("-bash: sudo: command not found") == ["sudo"]


@pytest.mark.parametrize("text", [
    "-bash: /usr/local/bin/thing: No such file",   # a missing FILE, not a tool
    "root@pve:~# qm config 106\nboot: order=scsi0",
    "",
])
def test_no_false_positives(text):
    assert find_missing_tools(text) == []


def test_operator_is_root_from_the_profile():
    assert operator_is_root(ROOT_ENV)
    assert not operator_is_root({"profile": "Operator runs as ubuntu@server via ssh"})


def test_sudo_is_dropped_on_the_root_host():
    """Root does not need sudo and PVE does not ship it, so the prefix is pure
    breakage. Stripping is safe: identical privileges either way."""
    out, notes = repair_unavailable_tools(
        "📍 On: the Proxmox host shell (root@pve)\n"
        "```bash\nsudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv "
        "&& sudo resize2fs /dev/ubuntu-vg/ubuntu-lv\n```", ROOT_ENV)
    assert "sudo" not in out
    assert notes and "dropped `sudo`" in notes[0]


def test_sudo_after_a_separator_is_also_dropped():
    """REGRESSION — a line-anchored rule stripped only the FIRST sudo, leaving
    `lvextend … && sudo resize2fs …`, which still died."""
    out, _ = repair_unavailable_tools(
        "📍 On: the Proxmox host shell (root@pve)\n"
        "```bash\nsudo a && sudo b ; sudo c | sudo d\n```", ROOT_ENV)
    assert "sudo" not in out


def test_prose_mentioning_sudo_is_never_touched():
    text = ("📍 On: the Proxmox host shell (root@pve)\n"
            "Note: you need sudo for normal users.\n```bash\nsudo qm stop 106\n```")
    out, _ = repair_unavailable_tools(text, ROOT_ENV)
    assert "you need sudo for normal users." in out
    assert "sudo qm stop" not in out


def test_in_guest_blocks_keep_their_sudo():
    """REGRESSION, and the dangerous one — a draft routinely spans the Proxmox
    host (root, no sudo) AND a guest console (ordinary user, sudo REQUIRED).
    Stripping session-wide removed sudo from `lvextend` blocks explicitly
    introduced with "inside the VM Console", which would fail permission-denied:
    worse than the bug it fixes."""
    text = ("📍 On: the Proxmox host shell (root@pve)\n```bash\nsudo qm stop 106\n```\n"
            "Now run these **inside the VM Console** (paladmin@palworld-server:~$):\n"
            "```bash\nsudo lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv\n```")
    out, _ = repair_unavailable_tools(text, ROOT_ENV)
    assert "qm stop 106" in out and "sudo qm stop" not in out   # host: stripped
    assert "sudo lvextend" in out                                # guest: preserved


def test_other_missing_tools_get_an_install_hint():
    """'How to attain it' is the actual question — silently emitting a command
    the operator cannot run is not an answer."""
    env = {"profile": "ubuntu@vm", "missing_tools": [{"tool": "jq", "host": ""}]}
    out, notes = repair_unavailable_tools("```bash\njq . /tmp/x.json\n```", env)
    assert out.strip().startswith("```")          # non-sudo tools are not rewritten
    assert any("apt-get install -y jq" in n for n in notes)


def test_find_unavailable_tools_ignores_prose():
    missing = [{"tool": "jq"}]
    assert find_unavailable_tools("You could use jq for this.", missing) == []
    assert find_unavailable_tools("```bash\njq . file\n```", missing)


def test_missing_tools_reach_the_prompt():
    """WITHOUT this the ledger never reaches the model and the engine only
    'knows' a tool is missing on the one turn whose error text mentions it —
    which is exactly how the SAME sudo command was emitted twice."""
    from app.modules.assist_render import render_environment_block
    block = render_environment_block(ROOT_ENV)
    assert "NOT AVAILABLE" in block
    assert "`sudo`" in block
    # §17.923 — scoped to THAT HOST: the unqualified "the operator is root
    # there, so simply omit it" read as a global instruction, and a live fix
    # then emitted guest `apt` commands with no sudo at all.
    assert "THAT HOST ONLY" in block
    assert "still needs sudo" in block


def test_lifecycle_commands_are_not_repeat_violations():
    """`qm stop` is legitimately repeated all through a troubleshooting run. The
    live caution banner led with "repeats something already tried (`qm stop
    106`)" — true, useless, and stapled above a fix whose content was correct.
    A gate that cries wolf teaches the operator to skip the real warnings."""
    from app.modules.assist_guide import find_repeated_failed
    window = "qm stop 106\n\nqm start 106\n\nqm destroy 106 --purge"
    assert find_repeated_failed("```bash\nqm stop 106\n```", window) == []
    assert find_repeated_failed("```bash\nqm start 106\n```", window) == []
    assert find_repeated_failed("```bash\nqm destroy 106 --purge\n```", window)
