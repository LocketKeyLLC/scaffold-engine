"""§17.1669 — "set up WireGuard wgN from <config>" in a container is the engine's own block (live, job 3cc24343 T7:
one draft without resolvconf, its redraft without the copy to /etc/wireguard/wg0.conf)."""
import os
import subprocess
from unittest.mock import MagicMock

from app.modules import supervised_runs as sr
from app.modules import wireguard_up as wu

T7 = {"node_key": "T7", "title": "Configure WireGuard wg0", "tool": "Shell",
      "prompt_template": ("Starts from T6. Install wireguard-tools, set up wg0 from /root/airvpn-bitmagnet.conf. "
                          "Enable wg-quick@wg0. Does NOT configure kill-switch.")}
REDRAFT = ["pct exec 109 -- apt-get update", "pct exec 109 -- apt-get install -y wireguard-tools resolvconf",
           "pct exec 109 -- systemctl enable wg-quick@wg0", "pct exec 109 -- systemctl start wg-quick@wg0"]


def test_the_live_step_is_claimed_with_the_guest_from_the_draft():
    assert wu.tunnel(T7, REDRAFT) == ("109", "wg0", "/root/airvpn-bitmagnet.conf")
    cmds, verify, note = wu.block(T7, REDRAFT)
    j = " ".join(cmds)
    assert "install -m 600 -o root -g root /root/airvpn-bitmagnet.conf /etc/wireguard/wg0.conf" in j
    assert "wireguard-tools resolvconf" in j and "latest-handshakes" in cmds[-1]
    assert cmds.index(next(c for c in cmds if "install -m 600" in c)) < cmds.index("pct exec 109 -- systemctl restart wg-quick@wg0")
    assert verify == ["pct exec 109 -- wg show wg0", "pct exec 109 -- ip -4 addr show wg0"]


def test_left_alone():
    assert wu.block({**T7, "title": "Configure nftables kill-switch"}, REDRAFT) is None
    assert wu.block({**T7, "prompt_template": "Install wireguard-tools. Enable wg-quick@wg0."}, REDRAFT) is None   # no source
    assert wu.block(T7, ["pct exec 109 -- x", "pct exec 105 -- y"]) is None                                    # which guest?
    assert wu.block({**T7, "title": "Verify WireGuard wg0 handshake"}, REDRAFT) is None


def test_the_frame_takes_it():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    draft = "## Run this\n\n```bash\n" + "\n".join(REDRAFT) + "\n```\n\n## Verify\n\n```bash\npct exec 109 -- wg show wg0\n```\n"
    frame = sr.frame_run(T7, draft, spec, policy, services=[],
                         inventory={"vms": {}, "cts": {"109": "running"}, "names": {"109": "bitmagnet"}})
    assert not frame["refused"] and frame["inputs"] == []
    assert any("/etc/wireguard/wg0.conf" in c for c in frame["commands"]), frame["commands"]


def _inner(cmd: str) -> str:
    """the script a `pct exec N -- sh -c '<script>'` runs, unquoted the way the shell would"""
    return subprocess.run(["bash", "-c", "eval \"set -- ${0#*sh -c }\"; printf '%s' \"$1\"", cmd],
                          capture_output=True, text=True).stdout


def test_the_place_and_handshake_scripts_run(tmp_path):
    cmds, _, _ = wu.block(T7, REDRAFT)
    for c in cmds:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True).returncode == 0, c
    hs = _inner(cmds[-1]).replace("seq 1 30", "seq 1 2").replace("sleep 1", "true")
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "wg").write_text("#!/bin/sh\necho \"peerkey\t$HS\"\n"); (bin_ / "wg").chmod(0o755)
    (bin_ / "systemctl").write_text("#!/bin/sh\necho status\n"); (bin_ / "systemctl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
    ok = subprocess.run(["sh", "-c", hs], capture_output=True, text=True, env={**env, "HS": "1792400000"})
    assert ok.returncode == 0 and "HANDSHAKE" in ok.stdout
    no = subprocess.run(["sh", "-c", hs], capture_output=True, text=True, env={**env, "HS": "0"})
    assert no.returncode == 1 and "never completed a handshake" in no.stdout
