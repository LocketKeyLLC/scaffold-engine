"""§17.1616 — each command of a block parses alone; the Proxmox firewall is host work.

Live (job 0fb4ea7f FW1, 2026-10-16): a two-line quoted sed program was split at its line break into two commands, neither
of which parses, and nothing refused it; and `pve-firewall restart` was refused as "changing the HOST instead of guest 110".
"""
from __future__ import annotations

from app.modules import runbook_templates as rt
from app.modules.supervised_runs import _SHAPE_REFUSALS, _WHOSE_GAP, a_command_that_does_not_parse, commands_never_reach_the_guest

LIVE = ["cp /etc/pve/firewall/cluster.fw /etc/pve/firewall/cluster.fw.bak.$(date +%Y%m%d%H%M%S)",
        "grep -q 'IN ACCEPT -p tcp -dport 3000' /etc/pve/firewall/cluster.fw || sed -i '/^\\[group ai\\]/,/^$/ { /IN DROP/i IN ACCEPT -p tcp -dport 3000",
        "}' /etc/pve/firewall/cluster.fw", "pve-firewall compile", "pve-firewall restart"]


def test_the_live_split_halves_are_refused_and_the_whole_lines_pass():
    out = a_command_that_does_not_parse(LIVE)
    assert [o["command"][:4] for o in out] == ["grep", "}' /"]
    assert "own runner call" in out[0]["why"]


def test_a_one_line_or_heredoc_free_block_passes():
    assert a_command_that_does_not_parse(["echo 'a b' | sed 's/a/b/'", "for i in 1 2; do echo $i; done"]) == []


def test_registered_for_redraft():
    assert any("does not parse on its own" in m for m in _SHAPE_REFUSALS)
    assert _WHOSE_GAP["does not parse on its own"] == "drafter"


def test_the_host_firewall_is_host_work():
    node = {"title": "Open ports 3000 and 8000 to the LAN in firewall group ai",
            "prompt_template": "edit /etc/pve/firewall/cluster.fw group ai (VM 110 uses it), then pve-firewall restart"}
    assert rt._HOST_ADMIN_RE.search(node["prompt_template"])
    assert commands_never_reach_the_guest(["pve-firewall restart"], node) == []


def test_a_placeholder_is_not_a_redirect():
    assert a_command_that_does_not_parse(["ping -c 1 -W 5 <AI_VM_IP>"]) == []


# §17.1617 — a sed program sed rejects
from app.modules.supervised_runs import a_sed_program_that_does_not_compile  # noqa: E402

LIVE_SED = ("grep -q 'IN ACCEPT -p tcp -dport 3000 -source 192.168.1.0/24' /etc/pve/firewall/cluster.fw || sed -i "
            "'/^\\[group ai\\]$/,/^$/ { /^IN DROP/ i IN ACCEPT -p tcp -dport 3000 -source 192.168.1.0/24' /etc/pve/firewall/cluster.fw")
GOOD_SED = ("grep -q 'dport 3000' /etc/pve/firewall/cluster.fw || sed -i '/^\\[group ai\\]$/,/^IN DROP$/ "
            "s|^IN DROP$|IN ACCEPT -p tcp -dport 3000 -source 192.168.1.0/24\\nIN DROP|' /etc/pve/firewall/cluster.fw")


def test_the_live_sed_is_refused_and_the_working_form_passes():
    out = a_sed_program_that_does_not_compile([LIVE_SED])
    assert len(out) == 1 and "unmatched" in out[0]["why"]
    assert a_sed_program_that_does_not_compile([GOOD_SED]) == []


def test_other_sed_shapes_pass():
    assert a_sed_program_that_does_not_compile(["sed -n '1,5p' /etc/hosts", "sed -E -i 's/(a+)/b/' f",
                                                "echo x | sed -e 's/x/y/' -e 's/y/z/'", "ls /tmp"]) == []
