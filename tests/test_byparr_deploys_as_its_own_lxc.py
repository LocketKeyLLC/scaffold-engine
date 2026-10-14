r"""§17.1535 — deploy Byparr (FlareSolverr's Camoufox-based drop-in) as its own new LXC.

The engine's own failure-research (§17.1201) on the T7 indexer-add diagnosed that FlareSolverr can
no longer clear 1337x/EZTV/… (Cloudflare detects the solved-cookie replay) and recommended Byparr.
Byparr is a true drop-in: same port 8191, same /v1 API. This capability deploys it the same way the
FlareSolverr deploy works — its own LXC (108, .27), 2 GB, Docker, the official image, a detached
install polled across the wait phases — so Prowlarr's FlareSolverr-type proxy can point at it.
"""
from __future__ import annotations

import subprocess

from app.modules.supervised_runs import (
    rewrite_deploy_byparr, _BYPARR_VMID, _BYPARR_IP, _BYPARR_INSTALL_BODY, host_only_inside_a_guest,
)

NODE = {"title": "Deploy byparr-proxy",
        "prompt_template": "Deploy byparr-proxy as a new LXC and wire it into Prowlarr as the indexer proxy for Cloudflare-protected indexers."}


def test_the_step_fires_and_emits_create_start_waits_last():
    cmds, verify, notes = rewrite_deploy_byparr(NODE, ["<model>"], ["chk"])
    assert notes and "§17.1535" in notes[0]["why"]
    assert len(cmds) == 9, "create + start + six waits + last"
    assert verify and "8191" in verify[0]


def test_the_create_is_host_side_2gb_idempotent_and_nests_no_guest_exec():
    create = rewrite_deploy_byparr(NODE, ["x"], ["y"])[0][0]
    assert create.startswith("sh -c 'if pct status %s" % _BYPARR_VMID)
    assert f"pct create {_BYPARR_VMID} " in create and "--hostname byparr" in create
    assert "--memory 2048" in create and f"ip={_BYPARR_IP}/24" in create and "nesting=1,keyctl=1" in create
    assert f"pct set {_BYPARR_VMID} --memory 2048" in create, "recover/right-size an existing one"
    assert "pct exec" not in create, "the host create script must not nest a guest exec"


def test_the_install_runs_the_official_byparr_image_detached():
    start = rewrite_deploy_byparr(NODE, ["x"], ["y"])[0][1]
    assert f"pct push {_BYPARR_VMID} /tmp/byparr_install.sh /root/.byparr_install.sh" in start
    assert "systemd-run --unit scaffold-byparr" in start and "--collect" not in start
    assert "ghcr.io/thephaseless/byparr:latest" in start and "docker rm -f byparr" in start
    assert "-p 8191:8191" in start, "same port as FlareSolverr (drop-in)"


def test_the_install_body_has_no_single_quote():
    assert "'" not in _BYPARR_INSTALL_BODY


def test_the_last_phase_fails_when_the_unit_failed_and_waits_use_a_counter():
    cmds = rewrite_deploy_byparr(NODE, ["x"], ["y"])[0]
    last = cmds[-1]
    assert "ExecMainStatus" in last and "exit 1" in last and "is-active scaffold-byparr" in last
    for c in cmds[2:]:
        assert "$SECONDS" not in c and "n=0; while [ $n -lt 30 ]" in c


def test_every_command_and_verify_parse_as_bash():
    cmds, verify, _ = rewrite_deploy_byparr(NODE, ["x"], ["y"])
    for c in cmds + verify:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True, text=True).returncode == 0


def test_it_does_not_trip_the_host_tool_gate_and_verify_is_read_only():
    from app.modules.assist_state_check import read_only_command
    cmds, verify, _ = rewrite_deploy_byparr(NODE, ["x"], ["y"])
    assert not host_only_inside_a_guest(cmds)
    v = verify[0]
    assert v.startswith("pct exec %s -- curl" % _BYPARR_VMID) and "python3" not in v and "$(" not in v
    assert read_only_command(v)


def test_the_transient_unit_is_not_flagged_as_unknown():
    from app.modules.supervised_runs import unsourced_service_name
    cmds, _, _ = rewrite_deploy_byparr(NODE, ["x"], ["y"])
    assert not unsourced_service_name(cmds, None, {}, NODE, units=["docker.service"])


def test_wording_variants_match_and_a_non_byparr_step_is_left_alone():
    for t in ("Install Byparr container", "Deploy byparr-proxy as a new LXC", "set up byparr service"):
        _, _, notes = rewrite_deploy_byparr({"title": t}, ["x"], ["y"])
        assert notes, t
    out, _, notes = rewrite_deploy_byparr({"title": "Add general TV indexers"}, ["x"], ["y"])
    assert out == ["x"] and not notes


def test_frame_run_runs_the_byparr_deploy():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_deploy_byparr(node, cmds, verify, services, inventory)" in src


def test_a_panel_feature_step_naming_byparr_is_not_hijacked():
    # §17.1542 — "Add FlareSolverr and Byparr services" is a control-panel FEATURE step (add them to
    # the panel's service list), developed on CT 111 — NOT a Byparr container deploy. The deploy
    # capability must not fire on it (it did, replacing the develop delivery with `pct create 108`).
    from app.modules.supervised_runs import rewrite_deploy_byparr, rewrite_deploy_flaresolverr
    node = {"title": "Add FlareSolverr and Byparr services",
            "prompt_template": "Add FlareSolverr (192.168.1.26:8191) and Byparr (192.168.1.27:8191) to the panel's service list."}
    out, _, notes = rewrite_deploy_byparr(node, ["x"], ["y"])
    assert out == ["x"] and not notes, "byparr-deploy must not hijack a panel feature step"
    out2, _, notes2 = rewrite_deploy_flaresolverr(node, ["x"], ["y"])
    assert out2 == ["x"] and not notes2, "flaresolverr-deploy must not hijack it either"
