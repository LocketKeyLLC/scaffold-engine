r"""§17.1516/1518 — "deploy FlareSolverr" done deterministically, as its own new LXC.

The stalled-downloads job (aefaef87) carried only the ASK1 decision — *how* FlareSolverr should
run — and no step that actually stands it up. This capability fires on a "deploy/install/create
FlareSolverr" step: a host-side `pct create` of the measured next-free container (107, .26 on
vmbr0, Debian 12, nesting+keyctl for Docker) then the official FlareSolverr Docker container on
:8191 inside it. The long install runs as a DETACHED systemd unit polled across wait phases
(§17.1518) — a raw `pct exec … <install>` ran synchronously and was cut by the 180 s budget.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import (
    rewrite_deploy_flaresolverr, _FS_VMID, _FS_IP, _FS_INSTALL_BODY, host_only_inside_a_guest,
)

NODE = {"title": "Deploy FlareSolverr",
        "prompt_template": "Deploy a FlareSolverr service as its own new LXC so Prowlarr can route Cloudflare indexers through it."}


def test_the_step_fires_and_emits_create_start_waits_last():
    cmds, verify, notes = rewrite_deploy_flaresolverr(NODE, ["<model>"], ["chk"])
    assert notes and "§17.1516" in notes[0]["why"]
    assert len(cmds) == 9, "create + start + six waits + last"
    assert verify


def test_the_create_is_host_side_idempotent_and_nests_no_guest_exec():
    # §17.1517 — a nested `pct exec` in the create script tripped the host-tool gate.
    create = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])[0][0]
    assert create.startswith("sh -c 'if pct status %s" % _FS_VMID)
    assert "already exists" in create and f"pct create {_FS_VMID} " in create and "--start 1" in create
    assert f"ip={_FS_IP}/24" in create and "bridge=vmbr0" in create and "nesting=1,keyctl=1" in create
    assert "pct exec" not in create, "the host create script must not nest a guest exec"


def test_the_install_is_pushed_as_a_file_and_run_as_a_detached_unit():
    # §17.1518 — a long install cannot ride one `pct exec` (180 s budget); it is pushed as a file and
    # launched as a transient systemd unit, polled across the wait phases.
    start = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])[0][1]
    assert f"pct push {_FS_VMID} /tmp/fs_install.sh /root/.fs_install.sh" in start
    assert "systemd-run --unit scaffold-fs" in start and "--collect" not in start, "keep ExecMainStatus readable"
    assert "bash /root/.fs_install.sh" in start


def test_the_install_runs_the_official_container_and_replaces_not_duplicates():
    start = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])[0][1]
    assert "docker rm -f flaresolverr" in start, "replace the old container, do not stack a second"
    assert "docker run -d --name flaresolverr --restart unless-stopped -p 8191:8191" in start
    assert "ghcr.io/flaresolverr/flaresolverr:latest" in start


def test_the_install_body_has_no_single_quote():
    # it rides inside a single-quoted host `sh -c '…'` heredoc; a single quote would break it.
    assert "'" not in _FS_INSTALL_BODY


def test_the_last_phase_fails_the_step_when_the_unit_failed():
    last = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])[0][-1]
    assert "ExecMainStatus" in last and "exit 1" in last and "is-active scaffold-fs" in last


def test_every_command_and_the_verify_parse():
    cmds, verify, _ = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])
    for c in cmds + verify:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_the_deploy_does_not_trip_the_host_tool_gate():
    cmds, _, _ = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])
    assert not host_only_inside_a_guest(cmds)


def test_the_verify_reads_the_port_through_the_read_only_channel():
    from app.modules.assist_state_check import read_only_command
    verify = rewrite_deploy_flaresolverr(NODE, ["x"], ["true"])[1]
    v = verify[0]
    assert v != "true" and "8191" in v
    assert "python3" not in v and "$(" not in v and read_only_command(v)


def test_a_non_flaresolverr_step_is_left_to_the_model():
    node = {"title": "Add general TV indexers", "prompt_template": "add indexers to prowlarr"}
    out, _, notes = rewrite_deploy_flaresolverr(node, ["x"], ["y"])
    assert out == ["x"] and not notes


def test_create_and_install_wordings_also_match():
    for t in ("Install FlareSolverr container", "Create the FlareSolverr LXC", "Set up FlareSolverr service"):
        _, _, notes = rewrite_deploy_flaresolverr({"title": t}, ["x"], ["y"])
        assert notes, t


def test_frame_run_runs_the_deploy_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_deploy_flaresolverr(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_deploy_flaresolverr(node, cmds") < src.index("read_on_the_machine(cmds")
