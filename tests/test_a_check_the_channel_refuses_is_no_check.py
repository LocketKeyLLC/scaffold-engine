"""§17.1390 — a check the channel will refuse is no check, and a step nothing checked is not done.

Live, 2026-10-06, and this one was mine the whole way down. §17.1382 gave the
engine a Jellyfin key read that uses `python3`; §17.1332 fills that read into
checks; the verify channel is READ-ONLY and its judge refuses `python3`. So the
frame was perfect::

    suggested: run   refused: 0   verify: 1   engine_fixed: 1

and the run recorded::

    supervised_run_done ... commands=1 confirmed_after_drop=False
    verify: (empty)
    last_verification_reason: "1 command(s) ran, all exited 0"

A step recorded done on one exit code — §17.1343's hollow success, through the
one door still open after nine entries of closing the others. The node was reset
to `pending` the moment it was read, because a false `done` on the operator's
plan is worse than an open step.

Two halves, because the guarantee was enforced at one end only:

* **frame time** — §17.1265 asks whether a check survived parsing; nothing asked
  whether the channel would agree to run it. The channel's own predicate answers
  that, so the gate cannot drift from the real decision.
* **run time** — §17.1233 judges the checks' ANSWERS and deliberately lets an
  ambiguous one leave the outcome alone. A check the runner never executed read
  identically to one that answered unclearly. It no longer does.
"""
from __future__ import annotations

import glob
import inspect
import json
import os

import pytest

from app.modules import machine_values as mv
from app.modules import supervised_runs as sr
from app.modules.assist_state_check import read_only_command
from app.modules.supervised_runs import (_WHOSE_GAP, a_check_the_channel_will_refuse,
                                         fill_what_the_engine_holds)

INV = {"names": {"101": "jellyfin"}}
MINTS = ["pct exec 101 -- sh -c 'python3 -c '\\''import secrets,sqlite3'\\'''"]


def _jellyfin_check() -> str:
    r = mv.readable_for("JELLYFIN_API_KEY")
    inner = f'curl -s -H "X-Emby-Token: $({r.read(None)})" "http://127.0.0.1:8096/Items"'
    return f"pct exec 101 -- sh -c {mv._sq(inner)}"


# ── frame time ──────────────────────────────────────────────────────────────

def test_the_live_check_is_refused_at_the_frame():
    """The exact check that was accepted, refused at run time and left the step
    falsely done."""
    out = a_check_the_channel_will_refuse([_jellyfin_check()])
    assert len(out) == 1
    why = out[0]["why"]
    assert "READ-ONLY" in why and "§17.1343" in why
    assert "find" in why and "cat" in why, "the remedy names shapes the channel takes"


def test_it_uses_the_channels_own_predicate():
    """Not a second opinion about what the channel allows — the same function,
    so the gate cannot drift from the decision it is predicting."""
    src = inspect.getsource(sr.a_check_the_channel_will_refuse)
    assert "from app.modules.assist_state_check import read_only_command" in src
    assert not read_only_command(_jellyfin_check())


@pytest.mark.parametrize("check", [
    "pct exec 101 -- curl -s http://127.0.0.1:8096/System/Info/Public",
    "pct exec 103 -- find /media/movies -type f",
    "pct exec 105 -- cat /var/lib/qbittorrent-nox/.config/qBittorrent/qBittorrent.conf",
    "pct list",
])
def test_a_check_the_channel_takes_is_left_alone(check):
    assert a_check_the_channel_will_refuse([check]) == []


def test_every_check_in_the_real_fixtures_still_passes():
    """Measured before the gate was written: 15 of 15. A gate that refused a
    check that already works would be worse than the bug."""
    seen = 0
    for p in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "add13*.json"))):
        for v in (json.load(open(p)).get("verify") or []):
            seen += 1
            assert a_check_the_channel_will_refuse([v]) == [], (os.path.basename(p), v[:80])
    assert seen >= 15


def test_blanks_are_not_this_gates_business():
    assert a_check_the_channel_will_refuse(["", "   ", None]) == []


def test_it_runs_in_frame_run_and_is_classified():
    assert "a_check_the_channel_will_refuse(verify)" in inspect.getsource(sr.frame_run)
    assert _WHOSE_GAP["the verify channel will refuse this check"] == "drafter"


# ── the engine must not fill a gap with an unrunnable check ────────────────

def test_the_filler_will_not_emit_a_check_the_channel_refuses(monkeypatch):
    """§17.1389's filler composed exactly such a check. It must now decline
    rather than close the refusal with something that cannot run."""
    monkeypatch.setattr(mv, "a_check_the_engine_can_write",
                        lambda cmds, inventory=None: [(_jellyfin_check(), "composed")])
    no_check = [{"command": "x", "why": "this block has no check at all"}]
    kept, verify, fills = fill_what_the_engine_holds(no_check, MINTS, [], INV)
    assert kept == no_check, "the refusal stands: the composed check would not run"
    assert verify == [] and fills == []


def test_the_filler_still_accepts_a_runnable_check(monkeypatch):
    monkeypatch.setattr(mv, "a_check_the_engine_can_write",
                        lambda cmds, inventory=None: [
                            ("pct exec 101 -- find /media/movies -type f", "composed")])
    no_check = [{"command": "x", "why": "this block has no check at all"}]
    kept, verify, _f = fill_what_the_engine_holds(no_check, MINTS, [], INV)
    assert kept == [] and len(verify) == 1


def test_an_unavailable_predicate_does_not_silently_drop_a_check():
    """Fail-soft must not mean fail-quiet in the direction that LOSES a check."""
    src = inspect.getsource(sr._channel_would_run)
    assert "return True" in src, "unknown: keep the check rather than drop it"


# ── run time: the half that actually mattered ─────────────────────────────

def test_a_run_where_no_check_ran_is_not_done():
    src = inspect.getsource(sr.resolve_decision) if hasattr(sr, "resolve_decision") else ""
    body = src or inspect.getsource(sr)
    assert '_never_ran = [x for x in (ran or []) if x.get("ran") is False]' in body
    assert "len(_never_ran) == len(verify_cmds)" in body
    assert "supervised_run_no_check_ran" in body


def test_the_downgrade_is_applied_after_the_others():
    """Ordering is the whole risk: `repeated_reason` is assigned between the
    verify block and the status write, so an earlier assignment would be
    clobbered and the step would go done anyway."""
    body = inspect.getsource(sr)
    i = body.index("_unverified = (")
    j = body.index("if _unverified and ok:")
    k = body.index("if ok or confirmed_after_drop:")
    assert i < j < k


def test_the_reason_tells_the_operator_what_happened():
    body = inspect.getsource(sr)
    i = body.index("_unverified = (")
    reason = body[i:i + 900]
    assert "were all refused by" in reason and "never ran" in reason
    assert "§17.1345" in reason, "it names the rule that says exit codes are not enough"


def test_an_ambiguous_answer_still_does_not_fail_the_work():
    """§17.1233's asymmetry is deliberate and must survive: only a CONTRADICTED
    verdict downgrades. This entry adds 'never ran', not 'unclear'."""
    body = inspect.getsource(sr)
    # the comment is line-wrapped in the source, so match fragments that survive it
    assert "Asymmetric on purpose" in body
    assert "leaves the outcome alone" in body
