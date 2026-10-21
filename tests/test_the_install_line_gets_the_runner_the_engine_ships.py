"""§17.1721 — the install line fetches the helper through GitHub's API and refuses any copy but the engine's own.

Live (2026-10-21, after #988 merged runner helper 24): the operator pasted the Machines page line; the host came back
on helper 23. `raw.githubusercontent.com/…/main/scripts/local_runner_mcp.py` kept serving the old file from its CDN
for over 20 minutes (a query string did not get past it), while the contents API answered the new one -- and the
line installed whatever it downloaded.
"""
from __future__ import annotations

import subprocess

from app.modules import engine_setup as es


def _run(line, tmp_path):
    return subprocess.run(["bash", "-c", line + " && echo INSTALLING"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, cwd=tmp_path).stdout.decode()


def test_a_stale_copy_installs_nothing(tmp_path):
    old = tmp_path / "old.py"
    old.write_text('#!/usr/bin/env python3\nHELPER_VERSION = "23"\n')
    out = _run(es.fetch_runner(f"file://{old}"), tmp_path)
    assert f"is not helper {es.expected_helper_version()}" in out and "INSTALLING" not in out


def test_the_engines_own_copy_installs(tmp_path):
    v = es.expected_helper_version()
    new = tmp_path / "new.py"
    new.write_text(f'#!/usr/bin/env python3\nHELPER_VERSION = "{v}"\n')
    assert "INSTALLING" in _run(es.fetch_runner(f"file://{new}"), tmp_path)


def test_the_public_copy_comes_through_the_api_with_the_raw_accept_header():
    line = es.fetch_runner()
    assert "raw.githubusercontent.com" not in line and "api.github.com/repos/LocketKeyLLC/scaffold-engine/contents/" in line
    assert "-H 'Accept: application/vnd.github.raw'" in line and "ref=main" in line
    assert "-H 'Accept" not in es.fetch_runner("http://192.168.1.43:8000/setup/runner/local_runner_mcp.py")


def test_every_line_the_operator_is_handed_carries_the_guard():
    ctx = {"token": "T", "script_url": es.RUNNER_SCRIPT_FALLBACK_URL}
    guard = f"grep -q '^HELPER_VERSION = \"{es.expected_helper_version()}\"'"
    assert guard in es.install_line(ctx) and guard in es.install_line(ctx, prefixes=["ANY"])
    for r in es.RECIPES:
        for _title, what in r.steps:
            assert "curl -fsSL {script_url}" not in what, r.id          # each template says {fetch_runner}
