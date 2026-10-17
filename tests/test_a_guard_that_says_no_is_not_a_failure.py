"""§17.1629 — a `<read> && <write>` line whose guard says no did nothing wrong.

Live (job 0fb4ea7f GPU0, 2026-10-17): the engine drafted, for "stop VM 110 if it is running, then detach
the P40",

    qm status 110 | grep -q running && qm stop 110
    qm set 110 --delete hostpci0

VM 110 was already stopped, so the first line exits 1 — the guard's "not needed" — and `run_block`, which stops
a write block at its first failure, would have ended the step before `qm set`. §17.1201 already let a READ's
non-zero exit pass; a guarded write is not a read, so it was not covered.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.modules import assist_supervised as sw

GPU0 = ["qm status 110 | grep -q running && qm stop 110", "qm set 110 --delete hostpci0", "qm start 110"]


def test_the_guard_of_a_guarded_write_is_found_and_nothing_else_is():
    assert sw.guarded_write(GPU0[0]) == "qm status 110 | grep -q running"
    for cmd in ("qm set 110 --delete hostpci0",                        # no guard
                "qm stop 110 && qm start 110",                         # the guard is a write
                "qm status 110 && qm config 110",                      # nothing written
                "qm status 110 | grep -q running && qm stop 110 && qm start 110",   # two writes: ambiguous
                "qm status 110 | grep -q running && qm stop 110 || true",
                "grep -q 'a && b' /etc/x && qm stop 110 ; qm start 110"):
        assert sw.guarded_write(cmd) == "", cmd
    assert sw.guarded_write("grep -q 'a && b' /etc/x && qm stop 110") == "grep -q 'a && b' /etc/x"


def _machine(tmp_path: Path, state: str, stop_fails: bool = False):
    """A runner that really runs each line in bash, against a `qm` that holds VM 110's state."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "state").write_text(state)
    qm = bindir / "qm"
    qm.write_text(f"""#!/bin/bash
echo "$*" >> {tmp_path}/log
case "$1" in
  status) echo "status: $(cat {tmp_path}/state)";;
  stop) {"echo 'VM 110 qmp command failed - timeout' >&2; exit 255" if stop_fails else f"echo stopped > {tmp_path}/state"};;
  start) echo running > {tmp_path}/state;;
  set) ;;
esac
""")
    qm.chmod(qm.stat().st_mode | stat.S_IEXEC)

    async def call(spec_, tool, args):
        p = subprocess.run(["bash", "-c", args["command"]], capture_output=True, text=True, timeout=10,
                           env={"PATH": f"{bindir}:/usr/bin:/bin"})
        r = MagicMock(); r.structured = None
        r.text = f"[exit {p.returncode}]\n{p.stdout}{p.stderr}"
        return r
    return call


def _spec():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "tok"}
    return spec


@pytest.mark.asyncio
async def test_a_stopped_vm_goes_on_to_the_detach(tmp_path):
    with patch("app.modules.mcp_client.call_tool", new=_machine(tmp_path, "stopped")):
        done = await sw.run_block(_spec(), GPU0)
    assert [d["command"] for d in done] == GPU0, "the guard's no did not end the block"
    assert done[0]["exit"] == 1 and done[0]["informational"] and not done[0]["ok"]
    assert "stop" not in (tmp_path / "log").read_text().split("set 110")[0].replace("status", "")
    assert "set 110 --delete hostpci0" in (tmp_path / "log").read_text()


@pytest.mark.asyncio
async def test_a_running_vm_is_stopped_and_a_failed_stop_still_stops_the_block(tmp_path):
    with patch("app.modules.mcp_client.call_tool", new=_machine(tmp_path, "running")):
        done = await sw.run_block(_spec(), GPU0)
    assert [d["ok"] for d in done] == [True, True, True]

    other = tmp_path / "b"; other.mkdir()
    with patch("app.modules.mcp_client.call_tool", new=_machine(other, "running", stop_fails=True)):
        done = await sw.run_block(_spec(), GPU0)
    assert len(done) == 1 and done[0]["exit"] == 255 and not done[0]["informational"], \
        "the guard said yes, so the write ran and failed: that is a failure"
    assert "set 110" not in (other / "log").read_text()
