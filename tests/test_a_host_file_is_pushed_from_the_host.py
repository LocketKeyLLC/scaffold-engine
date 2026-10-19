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


# --- the held-secret form: "write $NAME into container N at PATH" -------------------------------------------------

T6S = {"node_key": "T6", "title": "Write AirVPN config into CT 109",
       "prompt_template": ("Write the AirVPN WireGuard config the engine holds as the secret $AIRVPN_BITMAGNET_CONF into "
                           "CT 109 at /root/airvpn-bitmagnet.conf, mode 600, owned by root. Never print its contents. "
                           "Keep a dated copy of any file already there. Done when the file in CT 109 is mode 600 and "
                           "its Address line is 10.159.204.89/32 (grep the Address line only, never the keys). Do NOT "
                           "generate or re-provision.")}
HELD = ["AIRVPN_BITMAGNET_CONF", "MASS_PASSWORD"]


def test_a_held_secret_is_written_from_the_host():
    assert hfp.written_secret(T6S, HELD) == ("109", "AIRVPN_BITMAGNET_CONF", "/root/airvpn-bitmagnet.conf", "600")
    cmds, verify, note = hfp.secret_block(T6S, HELD)
    assert cmds[2].startswith('printf \'%s\' "<AIRVPN_BITMAGNET_CONF>" | pct exec 109 -- sh -c ')
    assert "tee" not in " ".join(cmds) and "cat /root" not in " ".join(cmds + verify)


def test_a_name_nothing_holds_or_two_names_are_left_alone():
    assert hfp.secret_block(T6S, ["MASS_PASSWORD"]) is None
    assert hfp.secret_block(T6S, []) is None
    two = {**T6S, "prompt_template": T6S["prompt_template"] + " Also $MASS_PASSWORD."}
    assert hfp.secret_block(two, HELD) is None
    assert hfp.secret_block({**T6S, "title": "Verify the AirVPN config"}, HELD) is None


def test_the_frame_takes_the_secret_form_and_the_runner_fills_it():
    from unittest.mock import MagicMock

    from app.modules import supervised_runs as sr
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": HELD, "held_logins": []}
    draft = ("## Run this\n\n```bash\npct exec 109 -- bash -c 'tee /root/airvpn-bitmagnet.conf <<EOF\nEOF\n"
             "chmod 600 /root/airvpn-bitmagnet.conf'\n```\n")                       # T6's second draft, live
    frame = sr.frame_run(T6S, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    joined = " ".join(frame["commands"])
    assert '"$AIRVPN_BITMAGNET_CONF"' in joined and "<AIRVPN_BITMAGNET_CONF>" not in joined
    assert "tee " not in joined and not frame.get("files")


def test_the_secret_block_runs_with_the_value_from_the_environment(tmp_path):
    from app.modules.supervised_runs import secret_ref_rewrite
    env = _fake_pct(tmp_path)
    (tmp_path / "guest").mkdir()
    dst = tmp_path / "guest" / "wg.conf"
    dst.write_text("old\n")
    value = "[Interface]\nAddress = 10.0.0.2/32\nPrivateKey = not-a-real-key\n\n"
    cmds, verify, _ = hfp.secret_block({"title": "Write the config", "prompt_template":
                                        f"Write $THE_CONF into CT 109 at {dst}, mode 600."}, ["THE_CONF"])
    cmds = [secret_ref_rewrite(c, "THE_CONF")[0] for c in cmds]
    res = _run(cmds, {**env, "THE_CONF": value})
    assert all(rc == 0 for rc, _ in res), res
    assert "WRITTEN" in res[-1][1] and "not-a-real-key" not in "".join(o for _, o in res)
    assert dst.read_text() == value and oct(dst.stat().st_mode & 0o777) == "0o600"
    assert [p.read_text() for p in (tmp_path / "guest").glob("wg.conf.bak.*")] == ["old\n"]
    assert not list((tmp_path / "guest").glob("*.scaffold-new"))


def test_the_template_reads_into_ct_at_path_as_a_secret_file_too():
    from app.modules import runbook_templates as rt
    assert rt.secret_files_from_text(T6S) == "/root/airvpn-bitmagnet.conf AIRVPN_BITMAGNET_CONF"
    assert rt.secret_files_from_text({"prompt_template": "write the stored AirVPN config ($AIRVPN_WG_CONF, never as "
                                      "a literal) to /etc/wireguard/wg0.conf"}) == "/etc/wireguard/wg0.conf AIRVPN_WG_CONF"


def test_a_held_name_is_a_secret_whatever_it_is_called():
    from app.modules.supervised_runs import apply_runner_secrets
    cmds, _, inputs, resolved, missing = apply_runner_secrets(
        ['printf \'%s\' "<THE_CONF>" | cat'], [], [{"name": "THE_CONF", "hint": "", "secret": False}], {"held": ["THE_CONF"]})
    assert inputs == [] and resolved == ["THE_CONF"] and missing == [] and '"$THE_CONF"' in cmds[0]
