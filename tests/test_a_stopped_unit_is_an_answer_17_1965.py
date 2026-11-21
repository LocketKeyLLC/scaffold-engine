"""§17.1965 — the develop kit's `unit(host, 'is-active', …)` answers a stopped unit's state instead of throwing, and
every remote error keeps the remote stdout. Driven in a real Node 20 with a stub `ssh` on PATH."""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
KIT = ROOT / "app" / "modules" / "develop_kit" / "node"
STUB = ROOT / "tests" / "fixtures" / "kit_ssh_stub"

_PROG = r"""
const r = require('/k/remote.js');
const host = { address: 'h', user: 'u' };
(async () => {
  const out = {};
  out.sync_stopped = r.unit(host, 'is-active', 'stopped.service');
  out.async_stopped = await r.promises.unit(host, 'is-active', 'stopped.service');
  out.async_running = await r.promises.unit(host, 'is-active', 'running.service');
  try { await r.promises.unit(host, 'is-active', 'dead'); out.dead = 'no error'; }
  catch (e) { out.dead = e.message; }
  try { await r.promises.unit(host, 'stop', 'stopped.service'); out.stop = 'no error'; }
  catch (e) { out.stop = 'threw'; }
  console.log(JSON.stringify(out));
})();
"""


def _node() -> list[str]:
    if shutil.which("docker") is None:
        pytest.skip("needs docker to run Node 20")
    if subprocess.run(["docker", "image", "inspect", "node:20-alpine"], capture_output=True).returncode != 0:
        pytest.skip("needs the node:20-alpine image")
    return ["docker", "run", "--rm", "-v", f"{KIT}:/k:ro", "-v", f"{STUB}:/stub:ro", "-e", "PATH=/stub:/usr/local/bin:/usr/bin:/bin",
            "node:20-alpine", "node", "-e", _PROG]


def test_a_stopped_unit_is_its_state_and_a_dead_ssh_still_throws():
    import json
    r = subprocess.run(_node(), capture_output=True, text=True, timeout=120)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["sync_stopped"] == "inactive" and out["async_stopped"] == "inactive"
    assert out["async_running"] == "active"
    assert "Connection refused" in out["dead"]
    assert out["stop"] == "threw"                       # only is-active reads a non-zero exit as an answer
