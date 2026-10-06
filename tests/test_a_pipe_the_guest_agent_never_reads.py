"""§17.1397 — input fed to `qm guest exec` without `--pass-stdin 1` never arrives.

Live, 2026-10-06. ADD137's redraft — "give the control panel an ssh key it can use
on VM 106" — installed the key with

    pct exec 111 -- sh -c 'cat /root/.ssh/id_ed25519.pub' | \\
        qm guest exec 106 -- sh -c 'cat >> /home/aedefruscio/.ssh/authorized_keys'

`qm guest exec` forwards STDIN only with `--pass-stdin 1` (default 0). The guest's
`cat` reads EOF, appends nothing, and the line exits 0. The engine's own
`run_in_vm_via_agent` template has always written `--pass-stdin 1`; no gate held a
drafted block to it. Caught reading the frame, before approval.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import supervised_runs as sr
from app.modules.runbook_templates import TEMPLATES
from app.modules.supervised_runs import a_pipe_the_guest_agent_never_reads as GATE

LIVE = ("pct exec 111 -- sh -c 'cat /root/.ssh/id_ed25519.pub' | qm guest exec 106 -- "
        "sh -c 'cat >> /home/aedefruscio/.ssh/authorized_keys'")


def test_the_live_add137_line_is_refused():
    out = GATE([LIVE])
    assert len(out) == 1
    assert "--pass-stdin 1" in out[0]["why"] and "exits 0 having done nothing" in out[0]["why"]


@pytest.mark.parametrize("line", [
    "qm guest exec 106 -- bash -c 'cat > /root/x.sh' < /tmp/x.sh",          # a redirect
    "qm guest exec 106 -- bash -c 'cat > /root/x' <<'EOF'",                 # a heredoc
    "echo hi | qm guest exec 106 --timeout 60 -- sh -c 'cat >> /tmp/y'",    # other flags, no pass-stdin
    "printf x | qm guest exec 106 --pass-stdin 0 -- sh -c 'cat'",           # explicitly off
])
def test_every_way_input_reaches_it(line):
    assert GATE([line])


@pytest.mark.parametrize("line", [
    "cat k.pub | qm guest exec 106 --pass-stdin 1 -- sh -c 'cat >> /root/.ssh/authorized_keys'",
    "qm guest exec 106 --timeout 60 --pass-stdin 1 -- bash -c 'cat > /root/s.sh' < /tmp/s.sh",
    "qm guest exec 106 --pass-stdin=true -- sh -c 'cat' < /tmp/x",
    "qm guest exec 106 -- sh -c 'wc -l < /home/u/.ssh/authorized_keys'",     # the < is INSIDE the guest command
    "qm guest exec 106 -- sh -c 'echo a | tee /tmp/x'",                     # the pipe is inside too
    "qm guest exec 106 -- uptime | python3 -c 'import json,sys; print(json.load(sys.stdin))'",  # its OUTPUT piped on
    "qm guest exec 106 -- uptime 2>&1",
    "# cat k | qm guest exec 106 -- sh -c 'cat >> x'",                       # a comment
])
def test_what_it_must_not_claim(line):
    assert GATE([line]) == [], line


def test_a_written_script_is_judged_too():
    assert GATE([], [{"path": "/tmp/k.sh", "content": "#!/bin/sh\n" + LIVE + "\n"}])


def test_the_engines_own_template_passes_its_own_rule():
    """The template is the rule's origin; a gate that refused it would be wrong."""
    for t in TEMPLATES:
        for content in (getattr(t, "files", None) or {}).values():
            assert GATE([], [{"path": "x", "content": content}]) == [], t.name


def test_wired_registered_and_the_drafters_gap():
    assert "a_pipe_the_guest_agent_never_reads(cmds, shape_files)" in inspect.getsource(sr.frame_run)
    refused = GATE([LIVE])
    assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP["is fed input here without `--pass-stdin 1`"] == "drafter"
