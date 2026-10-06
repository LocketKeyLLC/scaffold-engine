"""§17.1388 — the engine writes the check it already knows.

After nine entries of getting everything else right, ADD134's drafter would mint
Jellyfin's key, place the call where the key resolves, and then write **no
check** — and §17.1345 refused the block five drafts running for proving
nothing. That refusal is correct and it is the operator's own stance in code:

    a step recorded done on exit codes alone is how a change that did nothing
    passes

What was missing was never verification. It was COMPOSITION. The engine had
already written the exact check inside §17.1385's refusal text, down to the key
read — it could describe the check and not place it, so it kept asking a drafter
that would not take it.

Now it writes it: only for a service the registry knows, only on a guest this
block addresses, only as a pure read, and never silently — it lands in
`engine_fixed` beside every other correction (§17.1270).
"""
from __future__ import annotations

import inspect
import subprocess

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.machine_values import a_check_the_engine_can_write as COMPOSE

INV = {"names": {"101": "jellyfin", "103": "radarr", "105": "download-client"}}
#: a block that mints Jellyfin's key on guest 101 and names no check — the live shape
MINTS = ["pct exec 101 -- sh -c 'python3 -c '\\''import secrets,sqlite3'\\'''"]


def _bash_ok(cmd: str) -> bool:
    return subprocess.run(["bash", "-n"], input=cmd, text=True, capture_output=True).returncode == 0


def test_the_engine_composes_a_check_for_the_live_shape():
    out = COMPOSE(MINTS, INV)
    assert len(out) == 1
    check, why = out[0]
    assert check.startswith("pct exec 101 -- sh -c ")
    assert "X-Emby-Token" in check and "ApiKeys" in check


def test_the_composed_check_is_shell_valid():
    """It carries a read that is itself a quoted program, so the quoting is the
    whole risk (§17.1386). Asked of a shell, not of a regex."""
    check, _ = COMPOSE(MINTS, INV)[0]
    assert _bash_ok(check), check


def test_the_composed_check_compiles_through_the_engines_own_gate():
    """§17.1387 — and the gate that reads payloads must accept what the engine
    writes, or we are back to the engine refusing itself."""
    check, _ = COMPOSE(MINTS, INV)[0]
    assert sr.payload_will_not_compile([check]) == []


def test_the_composed_check_is_a_read():
    """The operator approves it, so it must not change anything."""
    check, _ = COMPOSE(MINTS, INV)[0]
    for mutate in (" -X POST", " -X PUT", " -X DELETE", "systemctl stop", "INSERT ", "rm "):
        assert mutate not in check, mutate
    assert "mode=ro" in check, "even the key read opens the database read-only"


def test_the_reason_says_what_it_reads_and_why():
    _check, why = COMPOSE(MINTS, INV)[0]
    assert "named no check" in why and "the engine wrote" in why
    assert "§17.1345" in why


# ── what it must not do ─────────────────────────────────────────────────────

def test_a_block_that_does_not_touch_the_service_gets_nothing():
    assert COMPOSE(["pct exec 103 -- sh -c 'true'"], INV) == []


def test_an_inventory_that_names_no_such_guest_gets_nothing():
    """Blindness invents nothing (§17.1289) — it will not guess a guest id."""
    assert COMPOSE(MINTS, {"names": {"103": "radarr"}}) == []
    assert COMPOSE(MINTS, {}) == []
    assert COMPOSE(MINTS, None) == []


def test_only_a_service_the_registry_knows_is_composed_for():
    for app in mv._CONFIRM:
        assert mv._SERVICES.get(app) is not None, app


def test_no_commands_is_no_work():
    assert COMPOSE([], INV) == []


# ── wired, and only as a last resort ────────────────────────────────────────

def test_it_runs_before_the_no_check_refusal():
    src = inspect.getsource(sr.frame_run)
    i = src.index("_mv.a_check_the_engine_can_write(cmds, inventory)")
    j = src.index("this block has no check at all")
    assert i < j, "the engine must try to WRITE a check before refusing for its absence"


def test_it_only_fires_when_the_draft_named_none():
    """A draft's own checks are never replaced: this is a last resort, not an
    opinion about checks the drafter did write."""
    src = inspect.getsource(sr.frame_run)
    i = src.index("_mv.a_check_the_engine_can_write(cmds, inventory)")
    guard = src[max(0, i - 400):i]
    assert "if cmds and not verify:" in guard


def test_the_addition_is_announced_not_silent():
    """§17.1270 — the operator approves what they are shown, so an added check
    is a correction on the frame like any other."""
    src = inspect.getsource(sr.frame_run)
    i = src.index("_mv.a_check_the_engine_can_write(cmds, inventory)")
    assert "_repairs = list(_repairs) + [{\"why\": _why}]" in src[i:i + 400]


def test_the_check_satisfies_the_gate_that_was_refusing():
    """End to end on the rule itself: with the composed check present, §17.1345
    has nothing to say — which is the whole point of the entry."""
    check, _ = COMPOSE(MINTS, INV)[0]
    assert sr.a_check_that_proves_nothing([check]) == []
