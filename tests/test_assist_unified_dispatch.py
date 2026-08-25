"""§17.771 (Phase 2) — the unified-decision dispatcher in the pipeline.

Pins `_decide_call` (HTTP → Decision | None, fail-soft) and `_dispatch_decision`
(one action → one handler, with the plan_impact=reshape override), without
touching a live session — every terminal handler is mocked and we assert which
one fired. Pipeline test: run with `--noconftest` (tests/conftest eager-loads app).
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipe():
    return Pipeline()


# ── _decide_call — HTTP → Decision | None ─────────────────────────────────────

def test_decide_call_returns_decision_on_200(pipe):
    sess = MagicMock()
    sess.post.return_value = _make_response(
        200, {"action": "ask", "confidence": "high", "query": "q"})
    with patch.object(_vendor, "_ss", return_value=sess):
        d = _vendor._decide_call(pipe, _SID, "help me", None, [])
    assert d["action"] == "ask" and d["confidence"] == "high"


def test_decide_call_none_on_404_valve_off(pipe):
    sess = MagicMock()
    sess.post.return_value = _make_response(404, {"detail": "disabled"})
    with patch.object(_vendor, "_ss", return_value=sess):
        d = _vendor._decide_call(pipe, _SID, "help me", None, [])
    assert d is None


def test_decide_call_none_on_missing_action(pipe):
    sess = MagicMock()
    sess.post.return_value = _make_response(200, {"confidence": "high"})  # no action
    with patch.object(_vendor, "_ss", return_value=sess):
        d = _vendor._decide_call(pipe, _SID, "x", None, [])
    assert d is None


# ── _dispatch_decision — one action → one handler ─────────────────────────────

_CASES = [
    ({"action": "advance"}, "assist_next"),
    ({"action": "skip"}, "assist_skip"),
    ({"action": "submit", "evidence": "done, 0 errors"}, "assist_submit"),
    ({"action": "fix", "error_text": "permission denied"}, "assist_fix_cmd"),
    ({"action": "ask", "query": "zfs vs lvm"}, "assist_research_cmd"),
    ({"action": "add_step"}, "assist_add_step_cmd"),
    ({"action": "note", "note_text": "only 2 NICs"}, "assist_note_cmd"),
    ({"action": "finalize"}, "assist_done"),
    ({"action": "status"}, "assist_status"),
    ({"action": "explain_plan"}, "assist_plan"),
    ({"action": "question"}, "assist_chat_turn"),
]


@pytest.mark.parametrize("decision,expected", _CASES)
def test_dispatch_routes_to_expected_handler(pipe, decision, expected):
    decision = {"confidence": "high", "plan_impact": "none", **decision}
    handlers = ["assist_next", "assist_skip", "assist_submit", "assist_fix_cmd",
                "assist_research_cmd", "assist_add_step_cmd", "assist_note_cmd",
                "assist_done", "assist_status", "assist_plan", "assist_chat_turn"]
    mocks = {h: MagicMock(return_value=[f"<{h}>"]) for h in handlers}
    with patch.object(_vendor, "_recall_node_key", return_value="T3"), \
         patch.multiple(_vendor, **mocks):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="the message", node_key="T3",
            chat_id="c1", history=[]))
    assert mocks[expected].called, f"{decision} should route to {expected}"
    # exactly one terminal handler fired
    assert sum(1 for m in mocks.values() if m.called) == 1
    assert f"<{expected}>" in out


def test_dispatch_reshape_overrides_to_note_path(pipe):
    """plan_impact=reshape takes the note→re-plan path even when action=ask."""
    decision = {"action": "ask", "query": "q", "confidence": "high",
                "plan_impact": "reshape"}
    with patch.object(_vendor, "_recall_node_key", return_value="T3"), \
         patch.object(_vendor, "assist_note_cmd",
                      return_value=["<note>"]) as note, \
         patch.object(_vendor, "assist_research_cmd",
                      return_value=["<ask>"]) as ask:
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="use zfs instead", node_key="T3",
            chat_id="c1", history=[]))
    assert note.called and not ask.called
    assert "<note>" in out


def test_surface_dispatches_action_then_appends_heads_up(pipe):
    """plan_impact=surface dispatches the action normally, THEN appends a gentle
    re-plan nudge (no hijack — that's reshape's job)."""
    decision = {"action": "ask", "query": "q", "confidence": "high",
                "plan_impact": "surface"}
    with patch.object(_vendor, "_recall_node_key", return_value="T3"), \
         patch.object(_vendor, "assist_research_cmd",
                      return_value=["<ask>"]) as ask, \
         patch.object(_vendor, "assist_note_cmd", return_value=["<note>"]) as note:
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="I only have 2 NICs",
            node_key="T3", chat_id="c1", history=[]))
    assert ask.called and not note.called   # action ran, no re-plan hijack
    assert "<ask>" in out
    assert "Heads-up" in out and "re-plan" in out  # gentle nudge appended


# ── §17.812 (audit C2) — deterministic veto + (I-4) decision node_key ──────────

def test_veto_reroutes_submit_to_fix_on_shell_error(pipe):
    """A high-confidence `submit` decision must be VETOED to `fix` when the
    message is a shell error — never auto-submit past a broken command."""
    decision = {"action": "submit", "confidence": "high", "plan_impact": "none",
                "evidence": "done"}
    handlers = {
        "assist_submit": MagicMock(return_value=["<submit>"]),
        "assist_fix_cmd": MagicMock(return_value=["<fix>"]),
    }
    with patch.object(_vendor, "_recall_node_key", return_value="T3"), \
         patch.object(_vendor, "_looks_like_shell_evidence", return_value=True), \
         patch.object(_vendor, "_looks_like_shell_error", return_value=True), \
         patch.multiple(_vendor, **handlers):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="-bash: scsi0: command not found",
            node_key="T3", chat_id="c1", history=[]))
    assert handlers["assist_fix_cmd"].called
    assert not handlers["assist_submit"].called
    assert "<fix>" in out


def test_no_veto_on_clean_submit(pipe):
    """A clean completion paste (not a shell error) still submits."""
    decision = {"action": "submit", "confidence": "high", "plan_impact": "none",
                "evidence": "done"}
    handlers = {
        "assist_submit": MagicMock(return_value=["<submit>"]),
        "assist_fix_cmd": MagicMock(return_value=["<fix>"]),
    }
    with patch.object(_vendor, "_recall_node_key", return_value="T3"), \
         patch.object(_vendor, "_looks_like_shell_evidence", return_value=False), \
         patch.object(_vendor, "_looks_like_shell_error", return_value=False), \
         patch.multiple(_vendor, **handlers):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="all set, the pool is created",
            node_key="T3", chat_id="c1", history=[]))
    assert handlers["assist_submit"].called
    assert not handlers["assist_fix_cmd"].called


def test_dispatch_prefers_decision_node_key(pipe):
    """§17.812 (I-4) — the node_key the /decide call resolved is used; the
    chatmap recall is NOT consulted (it's empty in this OWUI setup)."""
    decision = {"action": "submit", "confidence": "high", "plan_impact": "none",
                "node_key": "N9", "evidence": "done"}
    recall = MagicMock(return_value="WRONG")
    submit = MagicMock(return_value=["<submit>"])
    with patch.object(_vendor, "_recall_node_key", recall), \
         patch.object(_vendor, "_looks_like_shell_evidence", return_value=False), \
         patch.object(_vendor, "assist_submit", submit):
        list(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="ok done", node_key=None,
            chat_id="c1", history=[]))
    recall.assert_not_called()
    assert submit.call_args[0][2] == "N9"  # assist_submit(pipe, sid, nk, ev, ...)


# ── §17.812 (audit I-2 / C2 remainder) — unified-path parity gates ────────────

def test_advance_consults_tracker_and_retires(pipe):
    """A substantive "done, what's next" advance goes through /track so the
    in-flight step is retired SERVER-side; the tracker's `advanced` outcome
    renders the ✅ banner then presents the next step (audit I-2)."""
    decision = {"action": "advance", "confidence": "high", "plan_impact": "none",
                "node_key": "T3"}
    nxt = MagicMock(return_value=["<next>"])
    track = MagicMock(return_value={"action": "advanced", "retired_prior_step": "T3"})
    with patch.object(_vendor, "_track_progress", track), \
         patch.object(_vendor, "assist_next", nxt):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="ok that step is finished and it all works",
            node_key="T3", chat_id="c1", history=[]))
    track.assert_called_once()
    assert track.call_args[0][3] == "T3"  # _track_progress(pipe, sid, msg, nk, …)
    assert nxt.called
    assert "finished that step" in out and "<next>" in out


def test_advance_tracker_finalized_reaches_completion(pipe):
    """End-to-end via the unified path ONLY (audit I-2): the tracker finalizing
    on the last step renders the 🎉 completion + deliverable (assist_done), not
    a confusing "no step ready" from assist_next."""
    decision = {"action": "advance", "confidence": "high", "plan_impact": "none",
                "node_key": "T9"}
    done = MagicMock(return_value=["<done>"])
    nxt = MagicMock(return_value=["<next>"])
    with patch.object(_vendor, "_track_progress",
                      MagicMock(return_value={"action": "finalized"})), \
         patch.object(_vendor, "assist_done", done), \
         patch.object(_vendor, "assist_next", nxt):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="that last step is done and verified working",
            node_key="T9", chat_id="c1", history=[]))
    assert done.called and not nxt.called
    assert "completed the whole plan" in out and "<done>" in out


def test_advance_tracker_added_step_presents_it(pipe):
    """Tracker `added_step` outcome renders the ➕ banner + the new step."""
    decision = {"action": "advance", "confidence": "high", "plan_impact": "none",
                "node_key": "T3"}
    nxt = MagicMock(return_value=["<next>"])
    track = MagicMock(return_value={
        "action": "added_step", "step": {"title": "Fix the bridge"}})
    with patch.object(_vendor, "_track_progress", track), \
         patch.object(_vendor, "assist_next", nxt):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="I moved on and got the bridge half working",
            node_key="T3", chat_id="c1", history=[]))
    assert "Fix the bridge" in out and "<next>" in out


def test_advance_bare_verb_skips_tracker(pipe):
    """A bare "next" goes straight to assist_next — no tracker LLM call (the
    same word gate the cascade uses)."""
    decision = {"action": "advance", "confidence": "high", "plan_impact": "none",
                "node_key": "T3"}
    track = MagicMock()
    nxt = MagicMock(return_value=["<next>"])
    with patch.object(_vendor, "_track_progress", track), \
         patch.object(_vendor, "assist_next", nxt):
        "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="next", node_key="T3", chat_id="c1",
            history=[]))
    track.assert_not_called()
    assert nxt.called


def test_advance_tracker_proceed_falls_through_to_next(pipe):
    """Tracker says proceed (or errors → None): fall through to assist_next
    with no banner — fail-soft, same as the cascade."""
    decision = {"action": "advance", "confidence": "high", "plan_impact": "none",
                "node_key": "T3"}
    nxt = MagicMock(return_value=["<next>"])
    with patch.object(_vendor, "_track_progress",
                      MagicMock(return_value={"action": "proceed"})), \
         patch.object(_vendor, "assist_next", nxt):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="ok that part of the work is behind us now",
            node_key="T3", chat_id="c1", history=[]))
    assert nxt.called
    assert out == "<next>"


def test_question_howto_reroutes_to_research(pipe):
    """§17.733/763 restored on the unified path: a how-to the decision read as
    a bare `question` reaches research, not the step re-render."""
    decision = {"action": "question", "confidence": "high", "plan_impact": "none",
                "node_key": "T3"}
    research = MagicMock(return_value=["<research>"])
    chat = MagicMock(return_value=["<chat>"])
    with patch.object(_vendor, "assist_research_cmd", research), \
         patch.object(_vendor, "assist_chat_turn", chat):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="how do I set up the bridge interface?",
            node_key="T3", chat_id="c1", history=[]))
    assert research.called and not chat.called
    assert "<research>" in out


def test_question_plain_still_chats(pipe):
    """A plain clarification stays on assist_chat_turn (no over-trigger)."""
    decision = {"action": "question", "confidence": "high", "plan_impact": "none",
                "node_key": "T3"}
    research = MagicMock(return_value=["<research>"])
    chat = MagicMock(return_value=["<chat>"])
    with patch.object(_vendor, "assist_research_cmd", research), \
         patch.object(_vendor, "assist_chat_turn", chat):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="can you show that step again please",
            node_key="T3", chat_id="c1", history=[]))
    assert chat.called and not research.called


def test_checklist_request_reaches_checklist(pipe):
    """§17.707 restored on the unified path: "what do you need from me?" hits
    the live operator-input checklist, whatever the decision proposed."""
    decision = {"action": "question", "confidence": "high", "plan_impact": "none"}
    chk = MagicMock(return_value=["<checklist>"])
    chat = MagicMock(return_value=["<chat>"])
    with patch.object(_vendor, "_recall_node_key", return_value="T3"), \
         patch.object(_vendor, "assist_checklist_cmd", chk), \
         patch.object(_vendor, "assist_chat_turn", chat):
        out = "".join(_vendor._dispatch_decision(
            pipe, _SID, decision, msg="what do you need from me right now?",
            node_key="T3", chat_id="c1", history=[]))
    assert chk.called and not chat.called
    assert "<checklist>" in out
