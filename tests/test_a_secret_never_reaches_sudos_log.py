"""§17.1429 — a secret never reaches sudo's log; the filler opens without O_CREAT.

Live, 2026-10-08, ADD127: the run line `PANEL_PASSWORD="$PANEL_PASSWORD" bash …deliver.sh` was elevated as
`sudo -n --preserve-env=PANEL_PASSWORD PANEL_PASSWORD="$PANEL_PASSWORD" bash …`, and sudo logged
`ENV=PANEL_PASSWORD=<the value>` in the host's journal. And the python filler's `open(path, 'w')` was
refused even as root: the staged file belongs to the runner's user in sticky /tmp (`fs.protected_regular`).
"""
from __future__ import annotations

import asyncio
import sys
import pathlib
from types import SimpleNamespace

import pytest

from app.modules import assist_supervised as asup
from app.modules import develop as dv


@pytest.mark.parametrize("cmd,want", [
    ('PANEL_PASSWORD="$PANEL_PASSWORD" bash /tmp/x.sh', "bash /tmp/x.sh # $PANEL_PASSWORD"),
    ("A=$A B='${B}' python3 /tmp/y.py --z", "python3 /tmp/y.py --z # $A $B"),
    ("bash /tmp/x.sh", "bash /tmp/x.sh"),
    ('FOO="$BAR" bash /tmp/x.sh', 'FOO="$BAR" bash /tmp/x.sh'),          # not a self-assignment: untouched
    ('pct exec 111 -- curl -u panel:"$PANEL_PASSWORD" http://x', 'pct exec 111 -- curl -u panel:"$PANEL_PASSWORD" http://x'),
])
def test_self_assignments_become_references(cmd, want):
    assert asup.secret_prefixes_as_references(cmd) == want


def test_run_block_sends_the_rewritten_line_and_the_value_out_of_band(monkeypatch):
    from app.modules import mcp_client
    sent = []

    async def call_tool(spec, tool, payload):
        sent.append(payload)
        return SimpleNamespace(text="ok\n[exit 0]", structured=None, is_error=False)

    monkeypatch.setattr(mcp_client, "call_tool", call_tool)
    monkeypatch.setattr(asup, "runner_token", lambda spec: "t")
    asyncio.run(asup.run_block(object(), ['PANEL_PASSWORD="$PANEL_PASSWORD" bash /tmp/x.sh'],
                               env={"PANEL_PASSWORD": "s3cret", "OTHER": "no"}))
    assert sent[0]["command"] == "bash /tmp/x.sh # $PANEL_PASSWORD"
    assert sent[0]["env"] == {"PANEL_PASSWORD": "s3cret"}
    assert "s3cret" not in sent[0]["command"]


def test_the_runner_drops_them_when_it_elevates():
    sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "scripts"))
    import local_runner_mcp as r
    got = r.elevate('PANEL_PASSWORD="$PANEL_PASSWORD" bash /tmp/x.sh', "--preserve-env=PANEL_PASSWORD ")
    assert got == "sudo -n --preserve-env=PANEL_PASSWORD bash /tmp/x.sh"


def test_the_filler_opens_without_create():
    assert "'r+'" in dv._FILL_PROG and "'w'" not in dv._FILL_PROG and "$" not in dv._FILL_PROG
