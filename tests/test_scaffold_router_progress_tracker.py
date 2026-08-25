"""§17.754 — pipeline wiring for the progress tracker: on a substantive help turn,
consult the tracker and, when it inserts a step for an uncovered sub-task, present
that step's walkthrough instead of repeating the current step.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipe():
    return Pipeline()


class TestTrackProgressHelper:
    def test_parses_added_step(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(
            200, {"action": "added_step", "step": {"node_key": "ADD2", "title": "Configure network"}})
        with patch.object(_vendor, "_ss", return_value=sess):
            out = _vendor._track_progress(pipe, _SID, "help me with the network setup", "T13")
        assert out["action"] == "added_step"
        assert out["step"]["node_key"] == "ADD2"

    def test_http_error_returns_none(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(500, {"detail": "boom"})
        with patch.object(_vendor, "_ss", return_value=sess):
            assert _vendor._track_progress(pipe, _SID, "help me with the network", "T13") is None


class TestDispatchWiring:
    def _drive(self, pipe, track_result):
        """Run assist_nl_turn with an 'ask' classification and a stubbed tracker."""
        with patch.object(_vendor, "record_turn_bg"), \
             patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret",
                          return_value={"intent": "ask", "query": "network setup",
                                        "node_key": "T13"}), \
             patch.object(_vendor, "_track_progress", return_value=track_result) as tp, \
             patch.object(_vendor, "assist_next",
                          return_value=iter(["## 👉 Do this next\nConfigure the network…"])) as nxt, \
             patch.object(_vendor, "assist_research_cmd",
                          return_value=iter(["🔍 research answer"])) as research:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID, "i need help setting up the network on the installed server",
                node_key="T13", chat_id="chatA"))
        return out, tp, nxt, research

    def test_added_step_presents_new_walkthrough(self, pipe):
        out, tp, nxt, research = self._drive(
            pipe, {"action": "added_step", "step": {"node_key": "ADD2",
                                                    "title": "Configure guest network"}})
        tp.assert_called_once()
        assert "Configure guest network" in out       # the banner names the new step
        assert "Do this next" in out                   # …and presents its walkthrough
        nxt.assert_called_once()
        research.assert_not_called()                   # did NOT fall through to a repeat

    def test_advanced_presents_next_step(self, pipe):
        # §17.754 (#2) — tracker confirmed the current step is done → present next.
        out, tp, nxt, research = self._drive(pipe, {"action": "advanced",
                                                    "retired_prior_step": "T13"})
        tp.assert_called_once()
        assert "finished" in out.lower() or "moving on" in out.lower()
        nxt.assert_called_once()
        research.assert_not_called()

    def test_proceed_falls_through_to_normal_handling(self, pipe):
        out, tp, nxt, research = self._drive(pipe, {"action": "proceed"})
        tp.assert_called_once()
        research.assert_called_once()                  # normal ask handling ran
        nxt.assert_not_called()

    def test_short_turn_skips_tracker(self, pipe):
        # Below assist_tracker_min_words → tracker not consulted at all.
        with patch.object(_vendor, "record_turn_bg"), \
             patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret",
                          return_value={"intent": "ask", "query": "x", "node_key": "T13"}), \
             patch.object(_vendor, "_track_progress") as tp, \
             patch.object(_vendor, "assist_research_cmd", return_value=iter(["ans"])):
            "".join(_vendor.assist_nl_turn(pipe, _SID, "help network", node_key="T13", chat_id="c"))
        tp.assert_not_called()


class TestFinalizeRetiresInflightStep:
    """§17.839 (plan 8.0 — the §17.816 smoke's 'candidate polish'): a
    completion-evidence turn that classifies `finalize` while a step is
    in-flight must retire that step via the tracker FIRST, so 'done, and
    that was the last step' completes in ONE turn instead of summarizing
    with the step still `active` and needing a follow-up plain 'done'."""

    def _finalize(self, pipe, msg, track_result, nk="T13"):
        with patch.object(_vendor, "_track_progress",
                          return_value=track_result) as tp, \
             patch.object(_vendor, "assist_done",
                          return_value=iter(["📦 final summary"])) as done, \
             patch.object(_vendor, "assist_next",
                          return_value=iter(["## 👉 Do this next"])) as nxt:
            out = "".join(_vendor._emit_decision_action(
                pipe, _SID, {}, action="finalize", msg=msg, nk=nk,
                node_key=nk, chat_id="chatA", history=[]))
        return out, tp, done, nxt

    def test_evidence_turn_last_step_finalizes_in_one_turn(self, pipe):
        out, tp, done, nxt = self._finalize(
            pipe, "ran the last migration and verified the app boots — that was everything",
            {"action": "finalized"})
        tp.assert_called_once()
        assert "last step" in out          # 🎉 completion banner
        done.assert_called_once()          # summary rendered via the reconcile
        nxt.assert_not_called()

    def test_plan_disagrees_presents_next_step(self, pipe):
        """Operator thinks it's the last step; the plan has more → the
        reconcile advances, surfacing the mismatch instead of a summary
        that quietly shows an active step."""
        out, tp, done, nxt = self._finalize(
            pipe, "finished the deploy and confirmed everything works, we are done here",
            {"action": "advanced", "retired_prior_step": "T13"})
        tp.assert_called_once()
        nxt.assert_called_once()
        done.assert_not_called()

    def test_tracker_proceed_falls_through_to_summary(self, pipe):
        out, tp, done, nxt = self._finalize(
            pipe, "i think we are completely done with all of this now",
            {"action": "proceed"})
        tp.assert_called_once()
        done.assert_called_once()          # old behavior preserved
        assert "final summary" in out

    def test_bare_done_skips_tracker(self, pipe):
        """Short turns (below assist_tracker_min_words) go straight to the
        summary — same word gate as bare 'next' on the advance path."""
        out, tp, done, nxt = self._finalize(pipe, "we're done", {"action": "finalized"})
        tp.assert_not_called()
        done.assert_called_once()

    def test_no_inflight_step_goes_straight_to_summary(self, pipe):
        out, tp, done, nxt = self._finalize(
            pipe, "everything is finished and verified, wrap the session up please",
            {"action": "finalized"}, nk=None)
        tp.assert_not_called()
        done.assert_called_once()
