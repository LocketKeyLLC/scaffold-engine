r"""§17.1499 — a `$(pct exec GID … )` nested inside a command already in guest GID.

Live, job c75d77fa "Verify and Complete Media Pipeline" (2026-10-12, the operator's
media-pipeline test). Step T3v2's draft read the Jellyfin API token like this::

    pct exec 101 -- curl … -H "X-Emby-Token: $(pct exec 101 -- python3 -c '…jellyfin.db…')" …

The drafter had been shown both `<JELLYFIN_API_KEY>` and the canonical
`pct exec 101 -- sh -c 'curl … $(python3 -c …) …'` form (§17.1382), and sidestepped
the nested quoting by pulling the read out into its own `pct exec`. Two things are
wrong with that: `host_only_inside_a_guest` refuses the inner `pct` as a host tool
run inside a guest, and even allowed it the `$( … )` is expanded by the HOST shell
that runs the outer command — before `pct exec` — so the read runs on the host,
unprivileged, where `jellyfin.db` is not. The run parked as "I'll do it myself".

The fold is mechanical: the inner guest context EQUALS the outer, so the inner
wrapper is redundant. Strip it and promote the guest command to one `sh -c` payload
so the substitution runs in the guest's shell — byte-for-byte the form the engine
already fills for `<JELLYFIN_API_KEY>`.
"""
from __future__ import annotations

import subprocess

import pytest

from app.modules import machine_values as mv
from app.modules.supervised_runs import host_only_inside_a_guest

# The exact command that parked T3v2 on the live run.
T3V2 = (
    "pct exec 101 -- curl -s --fail-with-body "
    "-H \"X-Emby-Token: $(pct exec 101 -- python3 -c 'import sqlite3;"
    "print(sqlite3.connect(\"file:/var/lib/jellyfin/data/jellyfin.db?mode=ro\",uri=True)"
    ".execute(\"SELECT AccessToken FROM ApiKeys ORDER BY rowid LIMIT 1\").fetchone()[0])')\" "
    "http://127.0.0.1:8096/Library/VirtualFolders"
)


def test_the_live_t3v2_read_folds_into_the_guest_shell():
    cmds, verify, notes = mv.fold_redundant_nested_guest_exec([T3V2], [])
    out = cmds[0]
    assert out.startswith("pct exec 101 -- sh -c '"), "promoted to a single-guest sh -c payload"
    assert "$(python3 -c" in out and "$(pct exec" not in out, "the inner pct wrapper is stripped"
    assert "http://127.0.0.1:8096/Library/VirtualFolders" in out, "the request is unchanged"
    assert notes and "§17.1499" in notes[0]["why"]


def test_the_folded_command_is_valid_shell():
    out = mv.fold_redundant_nested_guest_exec([T3V2], [])[0][0]
    r = subprocess.run(["bash", "-n", "-c", out], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_host_tool_gate_refuses_the_original_and_accepts_the_fold():
    assert host_only_inside_a_guest([T3V2], []), "the nested pct must be refused before the fold"
    folded = mv.fold_redundant_nested_guest_exec([T3V2], [])[0]
    assert host_only_inside_a_guest(folded, []) == [], "nothing host-only remains after the fold"


def test_it_also_folds_a_nested_read_inside_an_existing_sh_c_payload():
    cmd = ("pct exec 101 -- sh -c 'curl -H \"T: $(pct exec 101 -- python3 -c "
           "'\"'\"'print(1)'\"'\"')\" http://127.0.0.1:8096/x'")
    out, _, notes = mv.fold_redundant_nested_guest_exec([cmd], [])
    assert "$(python3 -c" in out[0] and "$(pct exec" not in out[0]
    assert notes
    r = subprocess.run(["bash", "-n", "-c", out[0]], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_different_guest_is_left_alone():
    # $(pct exec 102 …) inside a `pct exec 101` command addresses ANOTHER machine —
    # not redundant, not this fold's business.
    cmd = 'pct exec 101 -- curl -H "H: $(pct exec 102 -- cat /x)" http://127.0.0.1:1'
    out, _, notes = mv.fold_redundant_nested_guest_exec([cmd], [])
    assert out == [cmd] and not notes


def test_a_plain_arr_command_is_untouched():
    cmd = 'pct exec 103 -- curl -s -H "X-Api-Key: $RADARR_API_KEY" http://127.0.0.1:7878/api/v3/indexer'
    out, _, notes = mv.fold_redundant_nested_guest_exec([cmd], [])
    assert out == [cmd] and not notes


def test_the_fold_is_idempotent():
    once = mv.fold_redundant_nested_guest_exec([T3V2], [])[0]
    twice, _, notes = mv.fold_redundant_nested_guest_exec(once, [])
    assert twice == once and not notes


def test_an_unbalanced_substitution_is_left_whole():
    cmd = 'pct exec 101 -- curl -H "T: $(pct exec 101 -- echo'
    out, _, notes = mv.fold_redundant_nested_guest_exec([cmd], [])
    assert out == [cmd] and not notes


def test_checks_are_folded_too():
    _, verify, notes = mv.fold_redundant_nested_guest_exec([], [T3V2])
    assert verify[0].startswith("pct exec 101 -- sh -c '") and "$(pct exec" not in verify[0]
    assert notes


def test_frame_run_folds_before_the_host_tool_gate():
    """The wiring: frame_run must fold ahead of the key substitution and the gate."""
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run) if hasattr(sr, "frame_run") else inspect.getsource(sr)
    assert "fold_redundant_nested_guest_exec(cmds, verify)" in src
    assert src.index("fold_redundant_nested_guest_exec") < src.index("read_on_the_machine(cmds")
