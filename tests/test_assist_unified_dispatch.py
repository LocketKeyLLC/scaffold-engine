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
