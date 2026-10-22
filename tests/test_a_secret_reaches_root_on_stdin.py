"""§17.1720 — a secret reaches the root shell on its stdin, never through sudo.

Live (host pve, 2026-10-21, read while finding why the host hung): every engine qBittorrent block was logged by sudo
as `scaffold-runner : PWD=/ ; USER=root ; ENV=MASS_PASSWORD=<the value> ; COMMAND=/usr/bin/bash -c '…'`. The runner
elevated with `sudo -n --preserve-env=MASS_PASSWORD bash -c …` (§17.1429/1430 believed preserving by NAME kept the
value out of the log); sudo logs every variable it preserves. Helper 24 writes the values to the root shell's stdin,
NUL-terminated, and the shell reads and exports them before the approved line runs. sudo does not log stdin.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "scripts"))
import local_runner_mcp as r  # noqa: E402

# live, the engine's qBittorrent read (its shape since §17.1746): the program on the command line, the value on stdin
QBT = ("pct exec 105 -- env QBT_FILTER='[]' python3 -c "
       "'import sys; print(\"PASS=\" + sys.stdin.readline().rstrip(\"\\n\")); print(\"REST=\" + repr(sys.stdin.read()))' "
       '<<< "$MASS_PASSWORD"')


def _stub_bin(tmp_path):
    """`sudo` records its argv and environment and then runs the line (as the caller); `pct exec N --` runs the rest."""
    b = tmp_path / "bin"
    b.mkdir()
    (b / "sudo").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$SEEN"; env > "$SEEN.env"\n'
                            '[ "$1" = -n ] && shift\nexec "$@"\n')
    (b / "pct").write_text('#!/bin/bash\nshift 2; [ "$1" = -- ] && shift\nexec "$@"\n')
    for f in ("sudo", "pct"):
        (b / f).chmod(0o755)
    return b


def test_sudo_sees_neither_the_value_nor_preserve_env_and_the_program_gets_it(tmp_path):
    b = _stub_bin(tmp_path)
    line = r.elevate(QBT, ["MASS_PASSWORD"])
    assert "preserve-env" not in line and "s3cr3t!!" not in line
    env = {k: v for k, v in os.environ.items() if k != "MASS_PASSWORD"}
    env.update(PATH=f"{b}:{os.environ['PATH']}", SEEN=str(tmp_path / "seen"))
    out = subprocess.run(["sh", "-c", line], env=env, input=r.stdin_for(["MASS_PASSWORD"], {"MASS_PASSWORD": "s3cr3t!!"}),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True).stdout.decode()
    assert "PASS=s3cr3t!!" in out                      # the program got the value
    assert "REST=''" in out                            # the here-string carried the value and nothing else
    assert "s3cr3t!!" not in (tmp_path / "seen").read_text()          # sudo's argv
    assert "s3cr3t!!" not in (tmp_path / "seen.env").read_text()      # sudo's environment


def test_two_secrets_keep_their_order_and_odd_bytes(tmp_path):
    b = _stub_bin(tmp_path)
    vals = {"A_KEY": "a b\nc'\"$x", "B_KEY": "ü-2"}
    line = r.elevate('printf "%s|%s" "$A_KEY" "$B_KEY"', ["A_KEY", "B_KEY"])
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "SEEN": str(tmp_path / "seen")}
    out = subprocess.run(["sh", "-c", line], env=env, input=r.stdin_for(["A_KEY", "B_KEY"], vals),
                         stdout=subprocess.PIPE, check=True).stdout.decode()
    assert out == "a b\nc'\"$x|ü-2"


def test_a_line_without_secrets_is_elevated_as_before():
    assert r.elevate("qm start 110") == "sudo -n qm start 110"
    assert r.elevate("qm status 110 | grep -q running || qm start 110").startswith("sudo -n bash -c 'qm status")
    assert r.stdin_for([], {}) == b""


def test_the_helper_says_it_is_the_fixed_one():
    assert int(r.HELPER_VERSION) >= 24


# ------------------------------------------------------------ the engine's end: an older runner is not offered the block

from app.modules import qbittorrent_read as qr  # noqa: E402
from app.modules import supervised_runs as sr  # noqa: E402

# live, the laptop engine's policy for pve-runner (2026-10-21; names only)
LIVE = {"helper": "23", "sudo": True, "secrets": [],
        "held": ["AIRVPN_WG_CONF", "MASS_PASSWORD", "PANEL_PASSWORD", "PROWLARR_API_KEY", "SONARR_API_KEY"]}


def test_the_live_qbittorrent_read_on_helper_23_is_refused():
    hit = sr.secrets_sudo_would_log([qr.command("105", [])], LIVE)
    assert len(hit) == 1 and "`$MASS_PASSWORD`" in hit[0]["why"] and "helper 24" in hit[0]["why"]


def test_helper_24_no_sudo_or_no_held_secret_passes():
    cmd = [qr.command("105", [])]
    assert sr.secrets_sudo_would_log(cmd, {**LIVE, "helper": "24"}) == []
    assert sr.secrets_sudo_would_log(cmd, {**LIVE, "sudo": False}) == []
    assert sr.secrets_sudo_would_log(["pct exec 104 -- echo $HOME $PATH"], LIVE) == []


def test_it_is_wired_into_the_frame():
    import inspect
    assert "secrets_sudo_would_log(list(cmds) + list(verify), policy)" in inspect.getsource(sr.frame_run)
