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
    assert sent[0]["command"] == "bash -c 'bash /tmp/x.sh # $PANEL_PASSWORD'"      # §17.1430 — and wrapped
    assert sent[0]["env"] == {"PANEL_PASSWORD": "s3cret"}
    assert "s3cret" not in sent[0]["command"]


def test_the_runner_drops_them_when_it_elevates():
    sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "scripts"))
    import local_runner_mcp as r
    got = r.elevate('PANEL_PASSWORD="$PANEL_PASSWORD" bash /tmp/x.sh', "--preserve-env=PANEL_PASSWORD ")
    assert got == "sudo -n --preserve-env=PANEL_PASSWORD bash /tmp/x.sh"


def test_the_filler_opens_without_create():
    assert "'r+'" in dv._FILL_PROG and "'w'" not in dv._FILL_PROG and "$" not in dv._FILL_PROG


# ── §17.1430 — a secret is expanded after sudo ──────────────────────────────

def test_a_command_carrying_a_secret_is_sent_wrapped(monkeypatch):
    from app.modules import mcp_client
    sent = []

    async def call_tool(spec, tool, payload):
        sent.append(payload)
        return SimpleNamespace(text="200\n[exit 0]", structured=None, is_error=False)

    monkeypatch.setattr(mcp_client, "call_tool", call_tool)
    monkeypatch.setattr(asup, "runner_token", lambda spec: "t")
    check = 'pct exec 111 -- curl -f -s -o /dev/null -w %{http_code} -u panel:"$PANEL_PASSWORD" http://127.0.0.1:3001/api/x'
    asyncio.run(asup.run_block(object(), [check], env={"PANEL_PASSWORD": "s3cret"}))
    asyncio.run(asup.run_block(object(), ["pct exec 111 -- true"], env={"PANEL_PASSWORD": "s3cret"}))
    assert sent[0]["command"].startswith("bash -c 'pct exec 111 -- curl") and "$PANEL_PASSWORD" in sent[0]["command"]
    assert "s3cret" not in sent[0]["command"]
    assert sent[1]["command"] == "pct exec 111 -- true"                  # no secret: untouched


def test_the_runner_elevates_it_as_a_whole_root_shell():
    sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "scripts"))
    import local_runner_mcp as r
    wrapped = asup.wrap_for_secrets('pct exec 111 -- curl -u panel:"$PANEL_PASSWORD" http://x')
    got = r.elevate(wrapped, "--preserve-env=PANEL_PASSWORD ")
    assert got.startswith("sudo -n --preserve-env=PANEL_PASSWORD bash -c ")
    # and the runner on its own (next reinstall) wraps an unwrapped line that carries a secret
    got2 = r.elevate('pct exec 111 -- curl -u panel:"$PANEL_PASSWORD" http://x', "--preserve-env=PANEL_PASSWORD ")
    assert got2.startswith("sudo -n --preserve-env=PANEL_PASSWORD bash -c ")


def test_bash_level_sudos_view_holds_the_name_not_the_value(tmp_path):
    """The calling shell runs the elevated line with a stub `sudo` that records its argv: the literal
    `$PANEL_PASSWORD` must reach it, never the value."""
    import os
    import subprocess
    sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "scripts"))
    import local_runner_mcp as r
    b = tmp_path / "bin"
    b.mkdir()
    (b / "sudo").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$SEEN"\n')
    (b / "sudo").chmod(0o755)
    line = r.elevate(asup.wrap_for_secrets('echo -u panel:"$PANEL_PASSWORD"'), "--preserve-env=PANEL_PASSWORD ")
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "SEEN": str(tmp_path / "seen"), "PANEL_PASSWORD": "s3cret"}
    subprocess.run(["bash", "-c", line], env=env, check=True)
    seen = (tmp_path / "seen").read_text()
    assert "s3cret" not in seen and "$PANEL_PASSWORD" in seen
