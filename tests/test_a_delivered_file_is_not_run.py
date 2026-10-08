"""§17.1420 — the ssh-key rule judges what the block RUNS, not what it delivers.

Live, 2026-10-08, ADD125 (the panel's Pi-hole view, guest 130): refused "nothing has put this host's key
on guest 130 … `ssh -o BatchMode=yes`" -- the only `ssh` was inside the engine's own kit file
`scaffold-kit/remote.js`, delivered with every version and never run by the block. One definition of
"the files a block runs" now serves this rule and §17.1415's API read-back gate.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import pathlib

from app.modules import runbook_preconditions as rp
from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD125 = json.loads((FIX / "add125_node_2026_10_08.json").read_text())
KIT = (pathlib.Path(__file__).parents[1] / "app/modules/develop_kit/node/remote.js").read_text()
INV = {"cts": {"111": "running", "130": "running"}, "vms": {}, "names": {"111": "control-panel", "130": "pihole"}}
STAGED_KIT = "/tmp/scaffold-dev-ADD125--opt--control-panel-backend--scaffold-kit--remote.js"


def _unmet(commands, files):
    return asyncio.run(rp.unmet(commands, None, plan=[], files=files, node=ADD125, inventory=INV))


def test_the_kit_delivered_is_not_an_ssh_the_block_makes():
    assert "BatchMode" in KIT or "ssh" in KIT                      # the shape that tripped it is really there
    refs = _unmet(["bash /tmp/scaffold-dev-ADD125--deliver.sh"],
                  [{"path": STAGED_KIT, "content": KIT},
                   {"path": "/tmp/scaffold-dev-ADD125--deliver.sh", "content": "pct push 111 a b\n"}])
    assert not any("nothing has put this host's key" in r["why"] for r in refs), refs


def test_an_ssh_the_block_runs_is_still_refused():
    refs = _unmet(["ssh -o BatchMode=yes root@192.168.1.30 pihole status"], [])
    assert any("nothing has put this host's key on guest" in r["why"] for r in refs)


def test_an_ssh_inside_a_script_the_block_runs_is_still_refused():
    refs = _unmet(["bash /tmp/fix.sh"], [{"path": "/tmp/fix.sh", "content": "ssh -o BatchMode=yes root@192.168.1.30 pihole status\n"}])
    assert any("nothing has put this host's key on guest" in r["why"] for r in refs)


def test_files_the_block_runs():
    files = [{"path": "/tmp/a.sh", "content": ""}, {"path": "/tmp/b.js", "content": ""}, {"path": "/tmp/c.py", "content": ""}]
    got = rp.files_the_block_runs(["bash /tmp/a.sh", "pct push 111 /tmp/b.js /opt/x/b.js", "python3 /tmp/c.py --x"], files)
    assert [f["path"] for f in got] == ["/tmp/a.sh", "/tmp/c.py"]


def test_one_definition_serves_both_rules():
    assert "files_the_block_runs(" in inspect.getsource(sr.changes_an_api_without_reading_it)
    assert "files_the_block_runs(commands, files)" in inspect.getsource(rp.unmet)
