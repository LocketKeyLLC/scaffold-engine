"""§17.1404 — a written file's `$NAME` is a shell reference only if a shell runs that file.

Live, 2026-10-06. ADD122's draft wrote `/tmp/add_palworld_route.py` and ran
`python3 /tmp/add_palworld_route.py`. Its route code — JavaScript, inside a
Python string — carries `${k}=${v}`, and §17.1348 refused `$k`: "read here and
nothing sets it". `variables_nothing_sets` read EVERY file a command names as
if a shell interpreted it.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules.supervised_runs import a_shell_runs_it, variables_nothing_sets

FIX = pathlib.Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "add122_python_file_not_shell_2026_10_06.json").read_text())
HELD = {"held": ["MASS_PASSWORD"]}


def test_the_live_draft_is_not_refused_for_k():
    out = variables_nothing_sets(LIVE["commands"], LIVE["files"], HELD)
    assert not [r for r in out if "`$k`" in r["why"] or "`$v`" in r["why"]], [r["why"][:80] for r in out]


@pytest.mark.parametrize("cmd,path,body,shell", [
    ("python3 /tmp/a.py", "/tmp/a.py", "import os\n", False),
    ("node /tmp/a.js", "/tmp/a.js", "", False),
    ("bash /tmp/a.sh start", "/tmp/a.sh", "", True),
    ("sh -e /tmp/a", "/tmp/a", "", True),
    ("source /tmp/env", "/tmp/env", "", True),
    (". /tmp/env && x", "/tmp/env", "", True),
    ("/tmp/run", "/tmp/run", "#!/bin/bash\necho\n", True),
    ("/tmp/run", "/tmp/run", "#!/usr/bin/env python3\n", False),
    ("/tmp/run", "/tmp/run", "echo no shebang\n", True),      # the shell runs it as sh
    ("cat /tmp/a.sh", "/tmp/a.sh", "", True),                  # a .sh name keeps the old judgment
])
def test_who_runs_it(cmd, path, body, shell):
    assert a_shell_runs_it(cmd, path, body) is shell


def test_a_shell_script_is_still_judged():
    files = [{"path": "/tmp/a.sh", "content": "curl -H \"K: $NOPE\" http://x\n"}]
    out = variables_nothing_sets(["bash /tmp/a.sh"], files, HELD)
    assert [r for r in out if "`$NOPE`" in r["why"]]


def test_a_python_files_dollar_is_left_alone():
    files = [{"path": "/tmp/a.py", "content": "s = '${NOPE}'\n"}]
    assert variables_nothing_sets(["python3 /tmp/a.py"], files, HELD) == []


# ── §17.1404b — an empty live config beside a template ───────────────────────

import asyncio  # noqa: E402

from app.modules import service_truth as st  # noqa: E402

_LIVE = "/opt/palworld/Pal/Saved/Config/LinuxServer/PalWorldSettings.ini"
_TPL = "/opt/palworld/DefaultPalWorldSettings.ini"


def _probe_with_size(size: str):
    show = ("Id=palworld.service\nUser=steam\nGroup=steam\nActiveState=active\nLoadState=loaded\n"
            "WorkingDirectory=/opt/palworld\n")

    async def probe(spec, c):
        if "systemctl show" in c:
            return True, show
        if "ls -1d" in c:
            return True, f"{_TPL}\n{_LIVE}\n"
        if "grep -c" in c:
            return True, f"{_TPL}:120\n{_LIVE}:0\n"
        if "stat -c" in c:
            return True, f"steam:steam 644 {_TPL}\nsteam:steam 644 {_LIVE}\n"
        if "wc -c" in c:
            return True, size
        return True, ""
    return probe


@pytest.mark.parametrize("size,empty", [("1\n", True), ("0", True), ("3880\n", False)])
def test_an_empty_live_file_beside_a_template_is_said(monkeypatch, size, empty):
    monkeypatch.setattr(st, "_probe", _probe_with_size(size))
    s = asyncio.run(st.read_services(object(), "106", mentioned=["palworld"], vm=True))[0]
    assert (s.empty_beside == _TPL) is empty
    says = s.says()
    assert ("so the server runs on the defaults in" in says) is empty
