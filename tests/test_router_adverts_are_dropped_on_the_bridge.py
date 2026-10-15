r"""§17.1571 — the gateway's IPv6 router advertisements are dropped on the Proxmox bridge, by the engine.

Operator (2026-10-15): "fix the IPv6 router advertisements at the gateway through the engine." The gateway is a
Spectrum router with no LAN admin page or API (the operator's standing note), so the nearest point the engine
controls is the bridge every guest hangs off: an nftables bridge table dropping nd-router-advert in the forward
hook (to the guests) and the input hook (to the host), persisted as its own oneshot unit. Measured: RAs from
fe80::4eab:f8ff:fed1:5491 reach every veth on vmbr0; nft is installed; /etc/nftables.conf is the Debian default
(`flush ruleset`), which is why the filter gets a unit of its own and never that file.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import (rewrite_drop_router_adverts_on_the_bridge, rewrite_disable_ipv6_in_guest,
                                         rewrite_prefer_ipv4_in_guest, checks_can_witness_the_change,
                                         _RAFILTER_FILE, _RAFILTER_UNIT)

STEP = {"node_key": "T2", "title": "Drop IPv6 router advertisements at the Proxmox bridge", "tool": "Shell",
        "description": "On the Proxmox host (pve), stop the gateway's IPv6 router advertisements (from fe80::4eab:f8ff:fed1:5491) "
                       "from reaching the guests on vmbr0 and the host: drop nd-router-advert frames on the bridge with nftables, "
                       "persisted across reboots, without touching pve-firewall or any guest."}


def test_the_step_is_rewritten_to_a_bridge_table_and_a_oneshot_unit_on_the_host():
    cmds, verify, notes = rewrite_drop_router_adverts_on_the_bridge(STEP, ["<model>"], ["chk"], [], {})
    assert notes and "§17.1571" in notes[0]["why"] and "Reversible" in notes[0]["why"]
    assert len(cmds) == 1 and not cmds[0].startswith("pct exec")          # the host itself
    c = cmds[0]
    assert "test -x /usr/sbin/nft ||" in c and _RAFILTER_FILE in c and f"/etc/systemd/system/{_RAFILTER_UNIT}" in c
    assert "table bridge scaffold_rafilter" in c and "hook forward" in c and "hook input" in c
    assert c.count("icmpv6 type nd-router-advert counter drop") == 2
    assert "ExecStartPre=-/usr/sbin/nft delete table bridge scaffold_rafilter" in c and "ExecStart=/usr/sbin/nft -f" in c
    assert "systemctl enable scaffold-rafilter.service && systemctl restart scaffold-rafilter.service" in c
    assert "<<" not in c and "flush ruleset" not in c                       # no heredoc; never the global ruleset
    assert "pve-firewall" not in c and "nftables.conf" not in c
    assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_the_verify_lists_the_table_and_ends_with_a_verdict_word_read_only():
    from app.modules.assist_state_check import read_only_command
    cmds, verify, notes = rewrite_drop_router_adverts_on_the_bridge(STEP, ["x"], ["y"], [], {})
    v = verify[0]
    assert v.startswith("nft list table bridge scaffold_rafilter;") and "systemctl is-active scaffold-rafilter.service" in v
    assert v.endswith("grep -q nd-router-advert && echo STATE-OK || echo STATE-WRONG")
    assert notes[0]["expect"] == {v: "STATE-OK"}
    assert read_only_command(v)
    assert subprocess.run(["bash", "-n", "-c", v], capture_output=True, text=True).returncode == 0
    assert checks_can_witness_the_change(cmds, verify) == ""              # the unit's keys are read back (cat)


def test_other_ipv6_steps_and_verify_titles_are_not_this():
    verify_step = {"title": "Verify router advertisements are dropped", "tool": "Shell",
                   "description": "nft list table bridge scaffold_rafilter shows the drop rule on the host"}
    assert not rewrite_drop_router_adverts_on_the_bridge(verify_step, ["x"], ["y"], [], {})[2]
    guest_step = {"title": "Disable IPv6 on container 101", "tool": "Shell", "description": "disable IPv6 on eth0 in the jellyfin guest"}
    assert not rewrite_drop_router_adverts_on_the_bridge(guest_step, ["x"], ["y"], [], {"names": {"101": "jellyfin"}})[2]
    assert not rewrite_disable_ipv6_in_guest(STEP, ["x"], ["y"], [], {})[2]           # no guest named: not the guest fix
    assert not rewrite_prefer_ipv4_in_guest(STEP, ["x"], ["y"], [], {})[2]
    nowhere = {"title": "Drop router advertisements", "tool": "Shell", "description": "stop the RAs"}   # no place named
    assert not rewrite_drop_router_adverts_on_the_bridge(nowhere, ["x"], ["y"], [], {})[2]


def test_frame_run_runs_the_rafilter_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_drop_router_adverts_on_the_bridge(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_drop_router_adverts_on_the_bridge(node, cmds") < src.index("read_on_the_machine(cmds")
