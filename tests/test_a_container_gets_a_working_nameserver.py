r"""§17.1519 — "give container N a working nameserver", done host-side.

Live (job aefaef87, 2026-10-12): the new FlareSolverr LXC (107) inherited the host's Tailscale
resolver (100.100.100.100), could not resolve public names, and its install hung on `apt-get
update` until the 180 s budget cut it — twice. The engine's precondition machinery detected the
dead resolver and inserted a "give container 107 a working nameserver" step, but drafted it to run
INSIDE the guest, where DNS is exactly what is broken. The fix is host-side: `pct set N
--nameserver` (the gateway the working media CTs use) then a reboot so resolv.conf regenerates.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import (
    rewrite_set_container_nameserver, rewrite_deploy_flaresolverr, host_only_inside_a_guest,
)

NODE = {"title": "Give container 107 a working nameserver",
        "prompt_template": "The container cannot resolve names; set a working DNS resolver so apt works."}


def test_it_fires_and_extracts_the_container_id():
    cmds, verify, notes = rewrite_set_container_nameserver(NODE, ["<model>"], ["chk"])
    assert notes and "§17.1519" in notes[0]["why"]
    assert cmds[0].startswith("sh -c 'pct set 107 --nameserver")


def test_the_fix_is_host_side_and_reboots_no_pct_exec():
    cmds, _, _ = rewrite_set_container_nameserver(NODE, ["x"], ["y"])
    cmd = cmds[0]
    assert "pct exec" not in cmd, "the fix must not run inside the guest, where DNS is broken"
    assert '--nameserver "192.168.1.1 1.1.1.1"' in cmd and "pct reboot 107" in cmd


def test_the_verify_resolves_a_public_name_and_is_read_only_safe():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_set_container_nameserver(NODE, ["x"], ["true"])
    v = verify[0]
    assert v == "pct exec 107 -- getent hosts deb.debian.org"
    assert "python3" not in v and "$(" not in v and read_only_command(v)


def test_commands_and_verify_parse():
    cmds, verify, _ = rewrite_set_container_nameserver(NODE, ["x"], ["y"])
    for c in cmds + verify:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_it_does_not_trip_the_host_tool_gate():
    cmds, _, _ = rewrite_set_container_nameserver(NODE, ["x"], ["y"])
    assert not host_only_inside_a_guest(cmds)


def test_a_step_without_a_container_id_is_left_alone():
    node = {"title": "Set a working nameserver"}  # no container id
    out, _, notes = rewrite_set_container_nameserver(node, ["x"], ["y"])
    assert out == ["x"] and not notes


def test_a_non_nameserver_step_is_left_to_the_model():
    node = {"title": "Add general TV indexers to container 102"}
    out, _, notes = rewrite_set_container_nameserver(node, ["x"], ["y"])
    assert out == ["x"] and not notes


def test_the_flaresolverr_create_now_sets_a_nameserver():
    # §17.1519 — a fresh create must not inherit the host's Tailscale resolver.
    create = rewrite_deploy_flaresolverr({"title": "Deploy FlareSolverr"}, ["x"], ["y"])[0][0]
    assert '--nameserver "192.168.1.1 1.1.1.1"' in create
    assert subprocess.run(["bash", "-n", "-c", create], capture_output=True, text=True).returncode == 0


def test_frame_run_runs_the_nameserver_fix():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_set_container_nameserver(node, cmds, verify, services, inventory)" in src


def test_frame_drops_the_stale_cannot_resolve_precondition_for_the_fixer():
    # §17.1519 — the precondition was computed from the ORIGINAL in-guest apt draft (which matched
    # the net-fetch pattern), before the rewrite replaced it with the host-side `pct set`. The fixer
    # step must not be refused by the very condition it fixes.
    from unittest.mock import MagicMock
    from app.modules import supervised_runs as sr
    runbook = ("## Run this\n```bash\npct exec 107 -- bash -c 'apt-get update'\n```\n"
               "## Verify\n- `pct exec 107 -- getent hosts deb.debian.org`\n")
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    stale = [{"command": "apt-get update",
              "why": "container 107 cannot resolve names: `getent hosts deb.debian.org` inside it printed nothing"}]
    frame = sr.frame_run({"node_key": "ADD1", "title": "Give container 107 a working nameserver"},
                         runbook, spec, {"allow": ["ANY"], "sudo": False, "helper": "22"},  # the live auto-mode channel
                         preconditions=stale)
    assert "run" in [o["id"] for o in frame["options"]], "the DNS fixer must be runnable"
    assert not any("cannot resolve names" in str(r.get("why", "")) for r in frame.get("refused", []))
