"""§17.1667 — "copy <host file> into container N" is the engine's own `pct push`, never an in-guest draft.

Live (job 3cc24343 T6, 2026-10-19): the draft ran inside CT 109 and wrote `<CONFIG_CONTENT>` over the target --
asking the operator to paste a WireGuard private key -- instead of copying the host's file.
"""
import os
import subprocess

from app.modules import host_file_push as hfp

T6 = {"node_key": "T6", "title": "Copy AirVPN config",
      "prompt_template": ("Copy the operator-provided config from host /root/airvpn-bitmagnet.conf into CT 109 "
                          "/root/airvpn-bitmagnet.conf, chmod 600. Do NOT generate or re-provision.")}


def test_the_live_step_is_a_push_from_the_host():
    cmds, verify, note = hfp.block(T6)
    assert "pct push 109 /root/airvpn-bitmagnet.conf /root/airvpn-bitmagnet.conf --perms 600" in cmds
    assert cmds[0].startswith("[ -f /root/airvpn-bitmagnet.conf ]")          # the host file must be there first
    assert not any("<" in c and "_CONTENT>" in c for c in cmds)               # nothing asks for the contents
    assert not any("cat " in c for c in cmds + verify)                        # and nothing prints them
    assert verify == ["pct exec 109 -- stat -c '%a %s %n' /root/airvpn-bitmagnet.conf"]
    assert "§17.1667" in note


def test_the_target_defaults_to_the_source_path_and_mode_is_optional():
    gid, src, dst, mode = hfp.pushed_file({"title": "Push the cert", "description":
                                           "Push it from the host /etc/ssl/x.pem into container 120."})
    assert (gid, src, dst, mode) == ("120", "/etc/ssl/x.pem", "/etc/ssl/x.pem", "")


def test_other_steps_are_left_alone():
    assert hfp.block({"title": "Configure WireGuard wg0", "prompt_template":
                      "Starts from T6. Set up wg0 from host /root/a.conf into CT 109 /etc/wireguard/wg0.conf"}) is None
    assert hfp.block({"title": "Copy the settings", "prompt_template": "copy /root/a into VM 110"}) is None
    assert hfp.block({"title": "Copy AirVPN config", "prompt_template": "copy it inside CT 109"}) is None
    assert hfp.block(None) is None


def _fake_pct(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    pct = bin_ / "pct"
    pct.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  status) echo 'status: running';;\n"
        "  push) cp \"$3\" \"$4\"; [ \"$5\" = --perms ] && chmod \"$6\" \"$4\"; true;;\n"
        "  exec) shift 3; \"$@\";;\n"
        "esac\n")
    pct.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}


def _run(cmds, env):
    out = []
    for c in cmds:
        r = subprocess.run(["bash", "-c", c], capture_output=True, text=True, env=env)
        out.append((r.returncode, r.stdout + r.stderr))
        if r.returncode:
            break
    return out


def test_the_block_runs_copies_keeps_the_old_file_and_compares(tmp_path):
    env = _fake_pct(tmp_path)
    (tmp_path / "host").mkdir()
    (tmp_path / "guest").mkdir()
    src, dst = tmp_path / "host" / "wg.conf", tmp_path / "guest" / "wg.conf"
    src.write_text("[Interface]\nPrivateKey = not-a-real-key\n")
    dst.write_text("old\n")
    cmds, verify, _ = hfp.block({"title": "Copy the config", "prompt_template":
                                 f"Copy from host {src} into CT 109 {dst}, chmod 600."})
    res = _run(cmds, env)
    assert all(rc == 0 for rc, _ in res), res
    assert "COPIED" in res[-1][1]
    assert "not-a-real-key" not in "".join(o for _, o in res)
    assert dst.read_text() == src.read_text()
    assert oct(dst.stat().st_mode & 0o777) == "0o600"
    assert [p.read_text() for p in (tmp_path / "guest").glob("wg.conf.bak.*")] == ["old\n"]
    assert _run(verify, env)[0][1].startswith("600 ")


def test_a_missing_host_file_fails_before_anything_is_touched(tmp_path):
    env = _fake_pct(tmp_path)
    (tmp_path / "guest").mkdir()
    dst = tmp_path / "guest" / "wg.conf"
    dst.write_text("old\n")
    cmds, _, _ = hfp.block({"title": "Copy the config", "prompt_template":
                            f"Copy from host {tmp_path}/nope.conf into CT 109 {dst}"})
    res = _run(cmds, env)
    assert res[0][0] != 0 and "is not on the Proxmox host" in res[0][1]
    assert len(res) == 1 and dst.read_text() == "old\n"


def test_the_frame_takes_it_and_asks_for_nothing():
    from unittest.mock import MagicMock

    from app.modules import supervised_runs as sr
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    draft = ("## Run this\n\n```bash\npct exec 109 -- bash -c 'cat > /root/airvpn-bitmagnet.conf <<EOF\n"
             "<CONFIG_CONTENT>\nEOF\nchmod 600 /root/airvpn-bitmagnet.conf'\n```\n")      # T6's draft, live
    frame = sr.frame_run(T6, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert any(c.startswith("pct push 109 ") for c in frame["commands"])
    assert "<CONFIG_CONTENT>" not in " ".join(frame["commands"]) and not frame.get("files")
