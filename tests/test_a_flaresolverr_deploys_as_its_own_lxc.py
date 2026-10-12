r"""§17.1516 — "deploy FlareSolverr" done deterministically, as its own new LXC.

The stalled-downloads job (aefaef87) carried only the ASK1 decision — *how* FlareSolverr should
run — and no step that actually stands it up. This capability fires on a "deploy/install/create
FlareSolverr" step and produces the real deploy: a host-side `pct create` of the measured next-free
container (107, .26 on vmbr0, Debian 12, nesting+keyctl for Docker) then the official FlareSolverr
Docker container inside it on :8191. Idempotent, with a read-only-channel-safe verify.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import rewrite_deploy_flaresolverr, _FS_VMID, _FS_IP, _FS_INSTALL_BODY

NODE = {"title": "Deploy FlareSolverr",
        "prompt_template": "Deploy a FlareSolverr service as its own new LXC so Prowlarr can route Cloudflare indexers through it."}


def test_the_step_fires_the_deploy():
    cmds, verify, notes = rewrite_deploy_flaresolverr(NODE, ["<model>"], ["chk"])
    assert notes and "§17.1516" in notes[0]["why"]
    assert len(cmds) == 2 and verify


def test_the_create_is_host_side_and_idempotent():
    cmds, _, _ = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])
    create = cmds[0]
    assert create.startswith("sh -c 'if pct status %s" % _FS_VMID)
    assert "already exists" in create, "a second run must not recreate the container"
    assert f"pct create {_FS_VMID} " in create and "--start 1" in create
    assert f"ip={_FS_IP}/24" in create and "bridge=vmbr0" in create and "nesting=1,keyctl=1" in create


def test_the_install_runs_the_official_container_and_replaces_not_duplicates():
    cmds, _, _ = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])
    install = cmds[1]
    assert install.startswith(f"pct exec {_FS_VMID} -- bash -c '")
    assert "docker rm -f flaresolverr" in install, "replace the old container, do not stack a second"
    assert "docker run -d --name flaresolverr --restart unless-stopped -p 8191:8191" in install
    assert "ghcr.io/flaresolverr/flaresolverr:latest" in install


def test_the_install_body_has_no_single_quote_so_the_async_wrap_is_clean():
    # the executor re-wraps `pct exec … bash -c '<body>'` into the §17.1317 detached unit; a single
    # quote in the body would break that single-quoted wrap.
    assert "'" not in _FS_INSTALL_BODY


def test_both_commands_and_the_verify_parse():
    cmds, verify, _ = rewrite_deploy_flaresolverr(NODE, ["x"], ["y"])
    for c in cmds + verify:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_the_verify_reads_the_port_through_the_read_only_channel():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_deploy_flaresolverr(NODE, ["x"], ["true"])
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
