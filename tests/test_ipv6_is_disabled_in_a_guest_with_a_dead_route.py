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
    # §17.1570c — loopback is stated, not assumed: `all` reset first, lo on, then the LAN interface off, in that order
    assert "net.ipv6.conf.all.disable_ipv6 = 1" not in c
    assert c.index("net.ipv6.conf.all.disable_ipv6 = 0") < c.index("net.ipv6.conf.lo.disable_ipv6 = 0") < c.index("net.ipv6.conf.eth0.disable_ipv6 = 1")
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


def test_the_planners_own_shapes_are_claimed_by_disable_not_by_prefer_ipv4():
    """§17.1570b — live (job eff80f98): the planner wrote the WORK into prompt_template as the sysctl keys and the
    drop-in's path; the phrase regex missed `disable_ipv6` and §17.1563 claimed the step with a gai.conf block."""
    t2 = {"node_key": "T2", "title": "Write sysctl config jellyfin", "tool": "Shell", "description": "",
          "prompt_template": "Starts from T1 decision. Writes the sysctl config file inside container 101 (jellyfin) only. "
                             "Contents: net.ipv6.conf.eth0.disable_ipv6=1, net.ipv6.conf.eth0.accept_ra=0, net.ipv6.conf.eth0.autoconf=0. "
                             "Does NOT apply settings, does NOT touch other containers."}
    t3 = {"node_key": "T3", "title": "Apply sysctl live jellyfin", "tool": "Shell", "description": "",
          "prompt_template": "Starts from T2. Runs sysctl -p /etc/sysctl.d/99-scaffold-disable-ipv6.conf inside container 101 only. Does NOT reboot."}
    for n in (t2, t3):
        cmds, _, notes = rewrite_disable_ipv6_in_guest(n, ["x"], ["y"], SERVICES, INV)
        assert notes and "§17.1570" in notes[0]["why"] and cmds[0].startswith("pct exec 101 -- "), n["title"]
        assert not rewrite_prefer_ipv4_in_guest(n, ["x"], ["y"], SERVICES, INV)[2], n["title"]
    t4 = {"node_key": "T4", "title": "Verify IPv6 disabled jellyfin", "tool": "Shell",
          "prompt_template": "Checks /proc/sys/net/ipv6/conf/eth0/disable_ipv6 reads 1 in container 101 only."}
    assert not rewrite_disable_ipv6_in_guest(t4, ["x"], ["y"], SERVICES, INV)[2]        # a verify is a read


def test_a_blanket_all_disable_is_refused_and_the_engines_own_block_is_not():
    """§17.1570d — live, the model's draft for 'Write sysctl config prowlarr' set all.disable_ipv6=1 and CT 102
    lost ::1 while the brief had said to keep loopback."""
    from app.modules.supervised_runs import disables_loopback_ipv6
    bad = ["pct exec 102 -- sh -c 'printf \"net.ipv6.conf.all.disable_ipv6 = 1\\nnet.ipv6.conf.eth0.disable_ipv6 = 1\\n\" > /etc/sysctl.d/99.conf && sysctl -p /etc/sysctl.d/99.conf'"]
    hit = disables_loopback_ipv6(bad)
    assert hit and "loopback" in hit[0]["why"] and "::1" in hit[0]["why"]
    assert disables_loopback_ipv6(["sysctl -w net.ipv6.conf.all.disable_ipv6=1"])
    assert disables_loopback_ipv6([], [{"path": "/tmp/x.sh", "content": "echo 1 > /proc/sys/net/ipv6/conf/all/disable_ipv6"}])
    cmds, _, _ = rewrite_disable_ipv6_in_guest(STEP, ["x"], ["y"], SERVICES, INV)
    assert disables_loopback_ipv6(cmds) == []                       # the engine's block sets all back to 0
    assert disables_loopback_ipv6(["# never set net.ipv6.conf.all.disable_ipv6 = 1\nsysctl -w net.ipv6.conf.eth0.disable_ipv6=1"]) == []


def test_a_step_that_says_same_as_t2_inherits_t2s_text_and_is_then_claimed():
    """§17.1570c — live: T5 'Writes the sysctl config file inside container 102 (prowlarr) only. Same contents as
    T2.' carried no IPv6 word of its own, so the model drafted it; T2's text is appended before the frame."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from app.modules.supervised_runs import inherit_referenced_step_text, _REFERENCED_STEP_RE
    t5 = {"node_key": "T5", "title": "Write sysctl config prowlarr", "tool": "Shell", "description": "",
          "prompt_template": "Starts from T1 decision. Writes the sysctl config file inside container 102 (prowlarr) only. "
                             "Same contents as T2. Does NOT apply settings."}
    assert not rewrite_disable_ipv6_in_guest(t5, ["x"], ["y"], SERVICES, INV)[2]          # on its own text: unclaimed
    assert [m.group(1) for m in _REFERENCED_STEP_RE.finditer(t5["prompt_template"])] == ["T2"]
    assert not list(_REFERENCED_STEP_RE.finditer("Starts from T1 decision. Writes the file."))   # a plain mention is not a reference
    rows = [{"node_key": "T2", "title": "Write sysctl config jellyfin", "description": None,
             "prompt_template": "Writes the sysctl config file inside container 101 (jellyfin) only. Contents: "
                                "net.ipv6.conf.eth0.disable_ipv6=1, net.ipv6.conf.eth0.accept_ra=0."}]
    res = MagicMock(); res.mappings.return_value.all.return_value = rows
    db = MagicMock(); db.execute = AsyncMock(return_value=res)
    out = asyncio.run(inherit_referenced_step_text(db, "job", t5))
    assert "[as in T2: Write sysctl config jellyfin" in out["description"] and "disable_ipv6=1" in out["description"]
    cmds, _, notes = rewrite_disable_ipv6_in_guest(out, ["x"], ["y"], SERVICES, INV)
    assert notes and cmds[0].startswith("pct exec 102 -- ")                              # now claimed, for 102
    again = asyncio.run(inherit_referenced_step_text(db, "job", out))
    assert again["description"].count("[as in T2:") == 1                                 # appended once
    import inspect
    from app.modules import supervised_runs as sr
    assert "return await inherit_referenced_step_text(db, job_id, node)" in inspect.getsource(sr.pending_hands_on)


def test_frame_run_runs_the_disable_ipv6_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_disable_ipv6_in_guest(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_disable_ipv6_in_guest(node, cmds") < src.index("read_on_the_machine(cmds")
