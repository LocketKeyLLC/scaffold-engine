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
    # `nft list` is a read form on the read-only channel (measured: the runner ran `nft list ruleset` for T1 of 9e1c2c68)
    assert v.startswith("nft list table bridge scaffold_rafilter;") and "systemctl is-active scaffold-rafilter.service" in v
    assert "systemctl is-enabled scaffold-rafilter.service" in v and "cat /etc/scaffold/rafilter.nft" in v
    assert v.endswith("&& echo STATE-OK || echo STATE-WRONG") and "grep -q nd-router-advert" in v
    assert "nft list table bridge scaffold_rafilter" in cmds[0]          # the counter is read in the RUN too
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


# ── §17.1573 — the plan's pieces and verifies of the same install are claimed too ───────────────────────────────
import json
from app.modules.supervised_runs import (rewrite_verify_router_adverts_dropped, a_capability_claims, mcp_needs_the_pause,
                                         is_mcp_write, mcp_write_runbook, _rafilter_check, _ANAPHORIC_RE, _NAMES_ITS_OBJECT_RE)


def _mcp(key, title, desc, tool, args):
    return {"node_key": key, "title": title, "description": desc, "tool": "MCP",
            "tool_config": json.dumps({"server": "pve-runner", "tool": tool, "args": args})}


T3 = _mcp("T3", "Write systemd unit file", "Small oneshot unit that loads the dedicated table at boot. RemainAfterExit=yes.",
          "write_file", {"path": "/etc/systemd/system/block-ra.service",
                         "content": "[Unit]\nDescription=Load nftables block_ra table\n[Service]\nType=oneshot\nExecStart=/usr/sbin/nft -f /etc/nftables.d/block-ra.nft\n"})
T4 = _mcp("T4", "Enable and start unit", "Loads the table now and ensures it loads at boot.",
          "run_supervised", {"command": "systemctl daemon-reload && systemctl enable block-ra.service && systemctl start block-ra.service"})
T5 = _mcp("T5", "Verify table and rule", "Confirms the dedicated table exists with the drop rule and its packet counter.",
          "run_readonly", {"command": "nft list table bridge block_ra"})
T7 = _mcp("T7", "Verify unit active enabled", "Confirms the unit is active and will run at boot.",
          "run_readonly", {"command": "systemctl is-active block-ra.service && systemctl is-enabled block-ra.service"})
T8 = _mcp("T8", "Verify pve-firewall untouched", "Confirms pve-firewall remains enabled and its per-guest security groups are intact.",
          "run_readonly", {"command": "pve-firewall status && cat /etc/pve/firewall/cluster.fw"})
T10 = _mcp("T10", "Verify guests untouched", "Confirms no guest was modified or restarted. Containers 105, 107, 108 remain as they were.",
           "run_readonly", {"command": "pct list"})


def test_a_write_file_node_is_a_write_that_parks_and_frames_as_the_shell_write_it_is():
    assert is_mcp_write(T3) and mcp_needs_the_pause(T3)
    assert not is_mcp_write(_mcp("x", "t", "d", "write_file", {"content": "no path"}))
    rb = mcp_write_runbook(T3, {}, {})
    assert rb and rb.startswith("## Run this\n```bash\nmkdir -p /etc/systemd/system && printf '%s\\n' ")
    assert "> /etc/systemd/system/block-ra.service" in rb and "ExecStart=/usr/sbin/nft -f" in rb


def test_the_pieces_of_the_install_are_claimed_by_the_same_capability():
    for step in (T3, T4):
        cmds, verify, notes = rewrite_drop_router_adverts_on_the_bridge(step, ["x"], ["y"], None, None)
        assert notes and "scaffold-rafilter.service" in cmds[0] and verify == [_rafilter_check()], step["node_key"]
    assert a_capability_claims(T4) and mcp_needs_the_pause(T4)


def test_a_verify_of_the_filter_reads_what_the_engine_installed_and_parks():
    for step in (T5, T7):
        cmds, verify, notes = rewrite_verify_router_adverts_dropped(step, ["x"], ["y"], None, None)
        assert notes and cmds == verify == [_rafilter_check()] and notes[0]["expect"] == {_rafilter_check(): "STATE-OK"}, step["node_key"]
        assert "block_ra" not in cmds[0] and a_capability_claims(step) and mcp_needs_the_pause(step)
    # verifies of OTHER things run as planned: no claim, no pause
    for step in (T8, T10):
        assert not rewrite_verify_router_adverts_dropped(step, ["x"], ["y"], None, None)[2], step["node_key"]
        assert not a_capability_claims(step) and not mcp_needs_the_pause(step), step["node_key"]
    assert not rewrite_drop_router_adverts_on_the_bridge(T5, ["x"], ["y"], None, None)[2]       # a verify is not the install


def test_the_filter_check_is_read_only_and_well_formed():
    from app.modules.assist_state_check import read_only_command
    assert read_only_command(_rafilter_check())
    assert subprocess.run(["bash", "-n", "-c", _rafilter_check()], capture_output=True, text=True).returncode == 0


def test_a_step_that_speaks_of_the_table_it_never_names_is_anaphoric():
    for own in ("Small oneshot unit that loads the dedicated table at boot.", "Loads the table now and ensures it loads at boot.",
                "Confirms the unit is active and will run at boot."):
        assert _ANAPHORIC_RE.search(own) and not _NAMES_ITS_OBJECT_RE.search(own), own
    for own in ("Write /etc/scaffold/rafilter.nft with the table", "Enable scaffold-rafilter.service, the unit",
                "nft list table bridge scaffold_rafilter shows the rule", "Same contents as T2 [as in T2: the file]"):
        assert _NAMES_ITS_OBJECT_RE.search(own), own
    assert not _ANAPHORIC_RE.search("Restart jellyfin and read its library count")


def test_in_guest_ipv6_steps_are_never_claimed_by_the_bridge_capability():
    guest_fix = {"title": "Disable IPv6 in container 102", "tool": "Shell",
                 "description": "In the prowlarr guest, stop accepting router advertisements: sysctl net.ipv6.conf.eth0.accept_ra=0, disable_ipv6=1"}
    guest_verify = {"title": "Verify IPv6 disabled on container 102", "tool": "Shell",
                    "description": "pct exec 102: eth0 disable_ipv6=1, accept_ra=0, no router advertisements accepted, ::1 intact"}
    for step in (guest_fix, guest_verify):
        assert not rewrite_drop_router_adverts_on_the_bridge(step, ["x"], ["y"], None, None)[2], step["title"]
        assert not rewrite_verify_router_adverts_dropped(step, ["x"], ["y"], None, None)[2], step["title"]
        assert not a_capability_claims(step)
