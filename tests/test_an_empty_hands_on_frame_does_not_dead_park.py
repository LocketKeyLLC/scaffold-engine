"""§17.1548b — an empty hands-on frame is not a dead park.

Live, control-panel job ae721781, 2026-10-14. The verify nodes T22v2 and T23v2
were sentence-fragments split from one old "Verify dark theme served" node (empty
descriptions). The drafter is non-deterministic on them — some rounds it drafts a
curl, most rounds it drafts NOTHING: no command, no check, and no shape-refusal
to explain it. That frame still parked as `kind="run"`, so `decide:run` returned
`not_runnable` 409 every time and the auto-driver spun forever on a decision that
could never be taken.

A VERIFY step that drafts nothing verifies nothing — it FAILS clean (the §17.1547
way: an absent check is a failed check), and the frontier moves on. Any OTHER
empty-and-unrefused step gets a stated refusal, so it parks as a real
myself/skip/redraft decision — never a dead `run`.
"""
from __future__ import annotations

from app.modules import supervised_runs as sr


def test_an_empty_verify_frame_fails_clean():
    disp = sr.empty_frame_disposition(
        {"commands": [], "verify": [], "refused": []},
        {"node_key": "T22v2", "node_type": "verify", "title": "Verify health returns JSON"})
    assert disp is not None and disp[0] == "fail"
    assert "nothing was verified" in disp[1] and "§17.1548b" in disp[1]


def test_an_observe_verb_title_counts_as_a_verify():
    # no node_type, but the title opens with an observe verb — still a verify.
    disp = sr.empty_frame_disposition(
        {"commands": [], "verify": [], "refused": []},
        {"node_key": "T23", "node_type": "", "title": "Verify that the served HTML"})
    assert disp is not None and disp[0] == "fail"


def test_an_empty_change_step_is_refused_not_failed():
    # a step that CHANGES something, drafted empty, parks a real decision (run withheld).
    disp = sr.empty_frame_disposition(
        {"commands": [], "verify": [], "refused": []},
        {"node_key": "T9", "node_type": "action", "title": "Install the health endpoint"})
    assert disp is not None and disp[0] == "refuse"
    assert "run` is withheld" in disp[1]["why"]


def test_a_frame_with_a_command_is_left_alone():
    assert sr.empty_frame_disposition(
        {"commands": ["pct exec 111 -- curl -s http://127.0.0.1:3001/api/health"], "verify": [], "refused": []},
        {"node_key": "T22v2", "node_type": "verify", "title": "Verify health"}) is None


def test_a_frame_with_a_check_is_left_alone():
    assert sr.empty_frame_disposition(
        {"commands": [], "verify": ["pct exec 111 -- systemctl is-active control-panel"], "refused": []},
        {"node_key": "T20", "node_type": "verify", "title": "Verify service active"}) is None


def test_a_frame_with_a_refusal_is_left_alone():
    # a frame that already carries a shape refusal has its own path — not this one.
    assert sr.empty_frame_disposition(
        {"commands": [], "verify": [], "refused": [{"why": "redirect"}]},
        {"node_key": "T5", "node_type": "verify", "title": "Verify something"}) is None
