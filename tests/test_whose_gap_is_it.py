"""§17.1389 — whose gap is each refusal, and the engine fills its own.

The operator, after §17.1388:

    "Then we should address it an create a composition verifier."

Four times in one day the engine refused a block for something it was itself
holding: §17.1332 asked for a key it could read, §17.1385 refused a call for a
key it could mint, §17.1387 refused its own escaping, §17.1388 refused a block
for a check it had already written into the refusal text. Four point fixes, and
nothing asking the question systematically.

Research first, over the real set — 68 markers in `_SHAPE_REFUSALS`:

    drafter  46   the block's shape is wrong; only a redraft fixes it
    machine  13   the machine contradicts it; neither drafter nor engine can fill
    engine    9   the engine holds what is missing

The third class came out of the data, not the design: 14 of those markers are
raised by `runbook_preconditions` / `machine_truth` and never by a shape gate at
all, so "the drafter's or the engine's" was the wrong question to start from.

The inventory is the point. A new gate cannot join `_SHAPE_REFUSALS` without
someone deciding whose gap it is, which is what stops this recurring in a fifth
costume ([[feedback_failsafes_are_a_registry]]).
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.supervised_runs import (_FILLERS, _SHAPE_REFUSALS, _WHOSE_GAP,
                                         fill_what_the_engine_holds, whose_gap)

INV = {"names": {"101": "jellyfin", "103": "radarr"}}
MINTS = ["pct exec 101 -- sh -c 'python3 -c '\\''import secrets,sqlite3'\\'''"]
NO_CHECK = [{"command": "x", "why": "this block has no check at all: nothing would confirm it"}]


# ── the inventory: the part that outlives any single filler ─────────────────

def test_every_refusal_marker_is_classified():
    """THE gate. Add a shape refusal without saying whose gap it is and this
    fails, naming it."""
    missing = [m for m in _SHAPE_REFUSALS if m not in _WHOSE_GAP]
    assert not missing, f"unclassified refusal markers: {missing}"


def test_nothing_is_classified_that_is_not_a_marker():
    """The other direction: a stale entry means a marker was renamed and the
    classification silently stopped applying."""
    extra = [m for m in _WHOSE_GAP if m not in _SHAPE_REFUSALS]
    assert not extra, f"classified but no longer a refusal marker: {extra}"


@pytest.mark.parametrize("bucket", ["drafter", "engine", "machine"])
def test_all_three_classes_are_used(bucket):
    """A classification where everything lands in one bucket would be a
    rubber stamp."""
    assert sum(1 for v in _WHOSE_GAP.values() if v == bucket) >= 5


def test_only_the_three_classes_exist():
    assert set(_WHOSE_GAP.values()) == {"drafter", "engine", "machine"}


def test_whose_gap_reads_a_real_refusal():
    assert whose_gap("this block has no check at all: nothing would confirm it") == "engine"
    assert whose_gap("guest 105 is stopped (`pct status`)") == "machine"
    assert whose_gap("this `curl` cannot report an HTTP error") == "drafter"
    assert whose_gap("something nothing in the engine ever said") is None


def test_every_filler_is_for_an_engine_gap():
    """A filler on a drafter gap would be the engine rewriting the drafter's
    work; on a machine gap it would be pretending a stopped guest is running."""
    for marker in _FILLERS:
        assert _WHOSE_GAP.get(marker) == "engine", marker


def test_every_filler_exists_and_is_callable():
    for marker, fn in _FILLERS.items():
        assert callable(getattr(mv, fn, None)), (marker, fn)


# ── the verifier: a fill is believed only once the rule agrees ──────────────

def test_a_fill_that_closes_the_rule_drops_the_refusal(monkeypatch):
    """§17.1390 — the fill must be a check the CHANNEL will run, so this uses a
    runnable composer. The Jellyfin read-back composed by §17.1388 carries
    `python3`, which the read-only channel refuses, and the filler now declines
    it (see `test_the_filler_will_not_emit_a_check_the_channel_refuses`)."""
    monkeypatch.setattr(mv, "a_check_the_engine_can_write",
                        lambda cmds, inventory=None: [
                            ("pct exec 101 -- find /media/movies -type f", "the engine wrote it")])
    kept, verify, fills = fill_what_the_engine_holds(NO_CHECK, MINTS, [], INV)
    assert kept == [], "the engine composed a runnable check, so the refusal is gone"
    assert len(verify) == 1 and "find /media/movies" in verify[0]
    assert fills and "the engine wrote" in fills[0]["why"]


def test_a_fill_with_nothing_to_give_leaves_the_refusal_standing():
    """No jellyfin in the inventory: nothing is composed, and the refusal must
    survive rather than be dropped on the strength of having tried."""
    kept, verify, fills = fill_what_the_engine_holds(NO_CHECK, MINTS, [], {"names": {}})
    assert kept == NO_CHECK and verify == [] and fills == []


def test_a_filler_that_does_not_close_the_rule_is_not_believed(monkeypatch):
    """The verifier itself. A filler that returns something useless must leave
    the refusal in place AND say so, not quietly pass."""
    monkeypatch.setattr(mv, "a_check_the_engine_can_write",
                        lambda cmds, inventory=None: [("", "I did nothing")])
    kept, _verify, _fills = fill_what_the_engine_holds(NO_CHECK, MINTS, [], INV)
    assert kept == NO_CHECK, "an empty check does not close the no-check rule"


def test_the_rule_is_re_asked_rather_than_reasoned_about():
    src = inspect.getsource(sr._engine_gap_remains)
    # the rule is re-asked about what would actually RUN: a blank entry is not a
    # check, which is how the first cut of this verifier read an empty string as
    # satisfaction (caught by `test_a_filler_that_does_not_close_the_rule…`).
    assert 'not [v for v in verify if str(v or "").strip()]' in src
    assert "return False" in src, "an unknown marker is never claimed closed"


def test_a_drafter_gap_is_never_filled():
    drafter = [{"command": "x", "why": "this `curl` cannot report an HTTP error"}]
    kept, verify, fills = fill_what_the_engine_holds(drafter, MINTS, [], INV)
    assert kept == drafter and verify == [] and fills == []


def test_a_machine_gap_is_never_filled():
    machine = [{"command": "x", "why": "guest 105 is stopped (`pct status`)"}]
    kept, verify, fills = fill_what_the_engine_holds(machine, MINTS, [], INV)
    assert kept == machine and verify == [] and fills == []


def test_no_refusals_is_no_work():
    assert fill_what_the_engine_holds([], MINTS, [], INV) == ([], [], [])


# ── wired as the one mechanism ──────────────────────────────────────────────

def test_frame_run_routes_the_no_check_refusal_through_the_filler():
    src = inspect.getsource(sr.frame_run)
    assert "fill_what_the_engine_holds(" in src
    # and §17.1388's inline special case is gone — one mechanism, not two
    assert "for _chk, _why in _mv.a_check_the_engine_can_write(" not in src


def test_the_fill_is_announced_on_the_frame():
    src = inspect.getsource(sr.frame_run)
    i = src.index("fill_what_the_engine_holds(")
    assert "_repairs = list(_repairs) + _engine_fills" in src[i:i + 300]
