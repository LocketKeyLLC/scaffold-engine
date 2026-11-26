"""§17.2035 — a stop run inside a container from the host (`pct exec N -- pkill …`) gets §17.1979's guard.

§17.1979 left it open: "a stop behind `pct exec N --` is not guarded: whether `pct exec` passes the inner status
through was not measured". The engine's own run records settle it (read-only, 2026-11-26): `pct exec 103 -- df -h
/downloads` and the same in 104 are recorded `rc=1` with df's "No such file or directory" -- the command's own
status -- while pct's OWN failure, `pct exec 111 -- pm2 start 1` on a stopped container, is recorded `rc=255`
("container '111' not running!"). So `|| [ $? -eq 1 ]` tolerates exactly the inner tool's "nothing to stop" and
still stops on pct's 255.

The stub `pct` below behaves as recorded: it runs the command after `--` and returns its status, or 255 when the
container is "not running".
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from app.modules import supervised_runs as sr

PCT_PASSES = 'case "$1" in exec) shift 2; [ "$1" = -- ] && shift; exec "$@";; esac\nexit 0\n'
PCT_DOWN = "echo \"container '105' not running!\" >&2\nexit 255\n"


def _repaired(body: str) -> tuple[str, list[dict]]:
    files, notes = sr.repair_no_op_stops_under_set_e([{"path": "/tmp/x.sh", "content": body}])
    return files[0]["content"], notes


def _run(body: str, tmp: pathlib.Path, stubs: dict[str, str]) -> subprocess.CompletedProcess:
    b = tmp / "bin"; b.mkdir(exist_ok=True)
    for name, text in {"systemd-run": f"echo \"systemd-run $*\" >> {tmp}/log\n", **stubs}.items():
        (b / name).write_text("#!/bin/sh\n" + text)
        (b / name).chmod(0o755)
    p = tmp / "s.sh"; p.write_text(body + "\n")
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0
    return subprocess.run(["bash", str(p)], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PATH": f"{b}:{os.environ['PATH']}"})


def _log(tmp: pathlib.Path) -> str:
    return (tmp / "log").read_text() if (tmp / "log").exists() else ""


SCRIPT = ("#!/bin/bash\nset -euo pipefail\n"
          "pct exec 105 -- pkill -f 'qq-old-indexer --port 9696'\n"
          "sudo pct exec 104 -- killall qq-bench\n"
          "systemd-run --unit=next true\n")


def test_both_stops_behind_pct_exec_are_guarded_and_said():
    body, notes = _repaired(SCRIPT)
    assert "pct exec 105 -- pkill -f 'qq-old-indexer --port 9696' || [ $? -eq 1 ]" in body
    assert "sudo pct exec 104 -- killall qq-bench || [ $? -eq 1 ]" in body
    assert len(notes) == 2


def test_the_repaired_script_runs_on_when_nothing_matched_in_the_containers(tmp_path):
    body, _ = _repaired(SCRIPT)
    out = _run(body, tmp_path, {"pct": PCT_PASSES, "pkill": "exit 1\n", "killall": "exit 1\n", "sudo": 'exec "$@"\n'})
    assert out.returncode == 0, out.stdout + out.stderr
    assert "systemd-run --unit=next" in _log(tmp_path)


def test_unrepaired_it_stops_at_the_first_pct_exec(tmp_path):
    """The bite: the drafted script, as it was left before this fix, ends at the in-container pkill's 1."""
    out = _run(SCRIPT, tmp_path, {"pct": PCT_PASSES, "pkill": "exit 1\n", "killall": "exit 1\n", "sudo": 'exec "$@"\n'})
    assert out.returncode == 1 and _log(tmp_path) == ""


def test_a_stopped_container_still_stops_the_script(tmp_path):
    """pct's own failure is 255, not the 1 tolerated: a container that is not running still ends the step."""
    body, _ = _repaired(SCRIPT)
    out = _run(body, tmp_path, {"pct": PCT_DOWN, "sudo": 'exec "$@"\n'})
    assert out.returncode != 0 and _log(tmp_path) == "", out.stdout + out.stderr
    assert "not running" in out.stderr


def test_a_real_pkill_error_inside_the_container_still_stops_it(tmp_path):
    body, _ = _repaired("#!/bin/bash\nset -e\npct exec 105 -- pkill --qq-bad x\nsystemd-run --unit=next true\n")
    out = _run(body, tmp_path, {"pct": PCT_PASSES, "pkill": "exit 2\n"})
    assert out.returncode != 0 and _log(tmp_path) == ""


@pytest.mark.parametrize("err, runs_on", [("Error response from daemon: No such container: qq_old", True),
                                          ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.", False)])
def test_docker_stop_in_a_container_asks_that_containers_daemon(tmp_path, err, runs_on):
    body, _ = _repaired("#!/bin/bash\nset -e\npct exec 120 -- docker stop qq_old\nsystemd-run --unit=next true\n")
    assert 'pct exec 120 -- docker stop qq_old || case "$(pct exec 120 -- docker container inspect qq_old' in body
    out = _run(body, tmp_path, {"pct": PCT_PASSES, "docker": f"echo '{err}' >&2\nexit 1\n"})
    assert (out.returncode == 0) is runs_on and ("systemd-run --unit=next" in _log(tmp_path)) is runs_on


def test_what_pct_wraps_that_is_not_a_bare_stop_is_left_alone():
    body = ("#!/bin/bash\nset -e\n"
            "pct exec 105 -- sh -c 'pkill a; true'\n"           # a quoted payload: §17.1979 leaves those
            "pct exec 105 -- systemctl stop sonarr\n"           # systemctl stop fails loudly, here as on the host
            "pct exec 105 pkill b\n"                            # no `--`: not the shape pct documents
            "pct exec x -- pkill c\n"                           # not a container id
            "pct exec 105 -- pkill d || true\n")                # already guarded
    after, notes = _repaired(body)
    assert notes == [] and after == body
