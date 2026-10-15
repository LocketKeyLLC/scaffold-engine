r"""§17.1570 — "disable IPv6 on container N" writes a persisted sysctl.d drop-in, applies it live, reads it back.

Operator (2026-10-15), after §17.1563 had treated the symptom on seven guests by preferring IPv4: "disable IPv6
on the LXCs through the engine" — remove the cause. Measured on CT 101: net0 carries no ip6 setting, eth0 has
accept_ra=1 autoconf=1 and two SLAAC addresses from the gateway's advertisements over a route that answers nothing.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import (rewrite_disable_ipv6_in_guest, rewrite_prefer_ipv4_in_guest,
                                         checks_can_witness_the_change, _named_guest)


class _Svc:
    def __init__(self, name, guest, ports, unit=""):
        self.name, self.guest, self.ports, self.unit = name, guest, ports, unit


SERVICES = [_Svc("jellyfin", "101", ("8096",), "jellyfin.service"), _Svc("pihole", "130", ("53",), "pihole-FTL.service")]
INV = {"names": {"101": "jellyfin", "102": "prowlarr", "111": "control-panel", "120": "caddy-proxy", "130": "pihole"}}
STEP = {"node_key": "T2", "title": "Disable IPv6 on container 101", "tool": "Shell",
        "description": "Disable IPv6 on container 101 (jellyfin): its IPv6 route from the gateway's router advertisements "
                       "answers nothing. Persist the change and apply it live without restarting services."}


def test_the_step_is_rewritten_to_a_persisted_sysctl_drop_in_applied_live():
    cmds, verify, notes = rewrite_disable_ipv6_in_guest(STEP, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1570" in notes[0]["why"] and "Reversible" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 101 -- sh -c '")
    c = cmds[0]
    assert "/etc/sysctl.d/99-scaffold-disable-ipv6.conf" in c and "sysctl -q -p" in c
    assert "net.ipv6.conf.eth0.disable_ipv6 = 1" in c and "net.ipv6.conf.default.disable_ipv6 = 1" in c
    assert "net.ipv6.conf.all.accept_ra = 0" in c and "net.ipv6.conf.eth0.autoconf = 0" in c
    assert "net.ipv6.conf.all.disable_ipv6" not in c          # loopback keeps IPv6: services bound to ::1 live on
    assert "systemctl restart" not in c and "reboot" not in c
    assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_the_verify_reads_the_change_back_read_only():
    from app.modules.assist_state_check import read_only_command
    cmds, verify, _ = rewrite_disable_ipv6_in_guest(STEP, ["x"], ["y"], SERVICES, INV)
    v = verify[0]
    assert "cat /proc/sys/net/ipv6/conf/eth0/disable_ipv6" in v and "accept_ra" in v
    assert "ip -6 addr show scope global | grep -c inet6" in v and "cat /etc/sysctl.d/99-scaffold-disable-ipv6.conf" in v
    assert read_only_command(v)
    assert subprocess.run(["bash", "-n", "-c", v], capture_output=True, text=True).returncode == 0
    # §17.1365 — the keys the block writes are read back by the check (the drop-in is cat'ed), so the pre-pass
    # is allowed to read it, and before the fix it reads 0 / 1 / 2 / "No such file" — a reading that can fail
    assert checks_can_witness_the_change(cmds, verify) == ""


def test_the_guest_comes_from_the_text_in_every_shape_and_the_interface_can_be_named():
    assert _named_guest("Disable IPv6 on container 120", SERVICES, INV)[:2] == ("caddy-proxy", "120")
    assert _named_guest("turn IPv6 off for pihole", SERVICES, INV) == ("pihole", "130", "pihole-FTL.service")
    assert _named_guest("Disable IPv6 in the jellyfin guest", SERVICES, INV) == ("jellyfin", "101", "jellyfin.service")
    assert _named_guest("Disable IPv6 on container 999", SERVICES, INV) == ("guest 999", "999", "")
    assert _named_guest("Disable IPv6 somewhere", SERVICES, INV) == ("", "", "")
    node = {"title": "Switch off IPv6 on container 111 (ens18)", "tool": "Shell", "description": "the panel guest"}
    cmds, _, notes = rewrite_disable_ipv6_in_guest(node, ["x"], ["y"], SERVICES, INV)
    assert notes and cmds[0].startswith("pct exec 111 -- ") and "net.ipv6.conf.ens18.disable_ipv6 = 1" in cmds[0]


def test_other_steps_are_left_alone_and_prefer_ipv4_is_not_this():
    verify_step = {"title": "Verify IPv6 is disabled on container 101", "tool": "Shell", "description": "check disable_ipv6 is 1"}
    assert not rewrite_disable_ipv6_in_guest(verify_step, ["x"], ["y"], SERVICES, INV)[2]
    prefer = {"title": "Make Jellyfin on container 101 reach TMDb", "tool": "Shell",
              "description": "Jellyfin's HTTP client prefers IPv6, which has no route; prefer IPv4 in the guest and restart jellyfin.service."}
    assert not rewrite_disable_ipv6_in_guest(prefer, ["x"], ["y"], SERVICES, INV)[2]
    assert rewrite_prefer_ipv4_in_guest(prefer, ["x"], ["y"], SERVICES, INV)[2]
    assert not rewrite_prefer_ipv4_in_guest(STEP, ["x"], ["y"], SERVICES, INV)[2]      # no "prefer ipv4"/timeout cue
    assert not rewrite_disable_ipv6_in_guest({"title": "Set onboot on VM 110", "description": "qm set 110 --onboot 1"}, ["x"], ["y"], SERVICES, INV)[2]


def test_frame_run_runs_the_disable_ipv6_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_disable_ipv6_in_guest(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_disable_ipv6_in_guest(node, cmds") < src.index("read_on_the_machine(cmds")
