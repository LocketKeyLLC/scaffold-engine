"""§17.1398 — a `[ … ] || { …; }` guard writes nothing, and each command is its own shell.

Live, 2026-10-06. ADD137's redraft, after §17.1397 refused its stdin pipe:

    KEY="$(pct exec 111 -- cat /root/.ssh/id_ed25519.pub)"
    [ -n "$KEY" ] || { echo 'FAILED: no public key in 111'; exit 1; }
    qm guest exec 106 -- sh -c "… printf '%s\\n' '$KEY' >> …/authorized_keys"

Two refusals, both with something false in them, and the engine gave the step
back ("suggested: myself"):

1. `variables_nothing_sets`: "`$KEY` is read here and nothing sets it: the block
   never assigns it". It does — on another line. `run_block` sends each command as
   its own runner call, so the refusal was RIGHT that `$KEY` arrives empty, and
   wrong about why; its remedy, "assign it in the block before use", was the shape
   the draft already had.
2. `commands_never_reach_the_guest`: the guard "would change the HOST instead of
   guest 106". `writing_segments` read `[ -n "$KEY" ]` and `{ echo …` as writes —
   `[` was not recognised as `test`, and a brace group's `{` stayed glued to its
   first command.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import assist_supervised
from app.modules import supervised_runs as sr
from app.modules.supervised_runs import (commands_never_reach_the_guest, variables_nothing_sets,
                                         writing_segments)

LIVE = [
    "pct exec 111 -- sh -c 'test -f /root/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N \"\" -f /root/.ssh/id_ed25519'",
    'KEY="$(pct exec 111 -- cat /root/.ssh/id_ed25519.pub)"',
    "[ -n \"$KEY\" ] || { echo 'FAILED: no public key in 111'; exit 1; }",
    "qm guest exec 106 -- sh -c \"cp -a /home/aedefruscio/.ssh/authorized_keys /home/aedefruscio/.ssh/authorized_keys.bak.\\$(date +%Y%m%d%H%M%S); printf '%s\\\\n' '$KEY' >> /home/aedefruscio/.ssh/authorized_keys\"",
]
NODE = {"title": "Give the control panel an ssh key it can use on VM 106 (palworld-server)", "description": ""}
INV = {"cts": {"111": "running"}, "vms": {"106": "running"}, "names": {"106": "palworld-server", "111": "control-panel"}}
HELD = {"held": ["MASS_PASSWORD", "RADARR_API_KEY"]}


# ── a guard is not a write ───────────────────────────────────────────────────

def test_the_live_guard_writes_nothing():
    assert writing_segments(LIVE[2]) == []


def test_so_the_live_guard_is_not_host_work_that_misses_the_guest():
    out = commands_never_reach_the_guest(LIVE[2:3], NODE, inventory=INV)
    assert out == [], out


@pytest.mark.parametrize("cmd", [
    "[[ -f /x ]] && echo ok",
    "[ -d /root/.ssh ] || { echo missing; exit 1; }",
    "{ echo a; echo b; }",
])
def test_tests_and_brace_groups_of_reads(cmd):
    assert writing_segments(cmd) == []


@pytest.mark.parametrize("cmd,write", [
    ("[ -f /x ] || { rm -rf /x; }", "rm -rf /x"),           # a write inside the group
    ("{ qm start 110; }", "qm start 110"),
    ("test -f /x || qm start 110", "qm start 110"),
])
def test_a_real_write_is_still_a_write(cmd, write):
    assert writing_segments(cmd) == [write]


def test_a_write_hidden_in_a_test_is_still_caught():
    """`[ "$(rm -rf /)" ]` is a test whose argument runs a write."""
    assert writing_segments('[ "$(rm -rf /)" ] || true')


# ── each command is its own shell ────────────────────────────────────────────

def test_the_premise_each_command_is_its_own_runner_call():
    """The rule rests on this; if run_block ever joined commands, the message must change."""
    src = inspect.getsource(assist_supervised.run_block)
    assert "for i, cmd in enumerate(commands, 1):" in src
    assert "call_tool(spec, WRITE_TOOL, payload)" in src


def test_the_live_key_refusal_names_the_real_reason():
    out = variables_nothing_sets(LIVE, [], HELD)
    keyed = [r for r in out if "`$KEY`" in r["why"]]
    assert keyed, out
    why = keyed[0]["why"]
    assert "set in a DIFFERENT command" in why
    assert 'KEY="$(pct exec 111' in why, "it names WHERE it was set"
    assert "its own shell" in why and "ONE command" in why
    assert "never assigns it" not in why, "the false claim is gone"


def test_a_name_set_nowhere_keeps_the_old_message_without_the_wrong_remedy():
    out = variables_nothing_sets(['curl -H "K: $NOPE" http://x'], [], HELD)
    assert out and "nothing sets it" in out[0]["why"]
    assert "assign it in the block before use" not in out[0]["why"]
    assert "its own shell" in out[0]["why"]


def test_set_and_used_in_one_command_is_fine():
    one = 'KEY="$(pct exec 111 -- cat /root/.ssh/id_ed25519.pub)" && [ -n "$KEY" ] && echo "$KEY"'
    assert [r for r in variables_nothing_sets([one], [], HELD) if "`$KEY`" in r["why"]] == []


def test_the_new_refusal_drives_a_redraft_and_is_the_drafters_gap():
    refused = [r for r in variables_nothing_sets(LIVE, [], HELD) if "DIFFERENT command" in r["why"]]
    assert sr.shape_retry_note({"kind": "run", "refused": refused})
    assert sr._WHOSE_GAP["but set in a DIFFERENT command"] == "drafter"
