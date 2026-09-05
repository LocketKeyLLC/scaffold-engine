"""§17.677 — pipeline-side surface-and-ask re-plan: proposal rendering in the
note confirmation, the deterministic yes/no confirm-gate, and the apply/discard
outcome render.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "eba60360-4153-4c7b-a0ee-c42d99768eb1"


@pytest.fixture
def pipe():
    return Pipeline()


class TestNoteRendersProposal:
    def test_note_with_proposal_surfaces_and_asks(self, pipe):
        proposal = {
            "note_kind": "constraint",
            "proposals": [
                {"node_key": "T1", "action": "revise",
                 "current_assumption": "TPM auto-unlock",
                 "proposed_change": "switch to passphrase LUKS"},
                {"node_key": "T10", "action": "drop",
                 "current_assumption": "secure boot needs TPM",
                 "proposed_change": "not possible without TPM"},
            ],
        }
        sess = MagicMock()
        sess.post.return_value = _make_response(
            200, {"recorded": True, "note": {"kind": "constraint", "text": "no TPM"},
                  "replan_proposal": proposal},
        )
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_note_cmd(
                pipe, _SID, "no TPM available", kind="constraint", node_key="T1",
            ))
        assert "📌 Noted (constraint):" in out
        assert "affects **2** pending steps" in out
        assert "**T1** (revise)" in out
        assert "**T10** (drop)" in out
        assert "switch to passphrase LUKS" in out
        assert "Apply these plan changes?" in out

    def test_note_without_proposal_keeps_plain_confirm(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(
            200, {"recorded": True, "note": {"kind": "constraint", "text": "only 2 NICs"}},
        )
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_note_cmd(
                pipe, _SID, "only 2 NICs", kind="constraint",
            ))
        assert "📌 Noted (constraint):" in out
        assert "carry this forward" in out.lower()
        assert "Apply these plan changes?" not in out


class TestReplanDecision:
    def test_yes_phrases_map_to_apply(self):
        for m in ("yes", "Apply", "do it", "go ahead", "sounds good."):
            assert _vendor._replan_decision(m) == "apply", m

    def test_no_phrases_map_to_discard(self):
        for m in ("no", "cancel", "leave it", "no thanks"):
            assert _vendor._replan_decision(m) == "discard", m

    def test_other_messages_are_none(self):
        for m in ("next", "what does T1 do?", "also add a DMZ", ""):
            assert _vendor._replan_decision(m) is None, m


class TestConfirmGate:
    def test_yes_with_pending_applies(self, pipe):
        with patch.object(_vendor, "fetch_pending_replan", return_value={"proposals": [1]}), \
             patch.object(_vendor, "assist_replan_confirm",
                          return_value=iter(["✅ Plan updated."])) as confirm, \
             patch.object(_vendor, "fast_classify_turn") as fct:
            out = "".join(_vendor.assist_nl_turn(pipe, _SID, "yes", node_key="T1"))
        assert "✅ Plan updated." in out
        _, kwargs = confirm.call_args if confirm.call_args and confirm.call_args.kwargs else (None, {})
        args = confirm.call_args.args
        assert "apply" in args
        fct.assert_not_called()  # gate short-circuited before classification

    def test_no_with_pending_discards(self, pipe):
        with patch.object(_vendor, "fetch_pending_replan", return_value={"proposals": [1]}), \
             patch.object(_vendor, "assist_replan_confirm",
                          return_value=iter(["👍 Left the plan unchanged."])) as confirm:
            out = "".join(_vendor.assist_nl_turn(pipe, _SID, "no", node_key="T1"))
        assert "unchanged" in out
        assert "discard" in confirm.call_args.args

    def test_yes_without_pending_falls_through(self, pipe):
        # No pending proposal → 'yes' is not a confirm; normal classification runs.
        with patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "fast_classify_turn", return_value="advance"), \
             patch.object(_vendor, "assist_next",
                          return_value=iter(["next step"])) as nxt:
            out = "".join(_vendor.assist_nl_turn(pipe, _SID, "yes", node_key="T1"))
        assert "next step" in out
        nxt.assert_called_once()


class TestPivotDetector:
    # §17.679 — a pivot/redirection must route to the note→re-plan path, never a
    # bare re-render of the current step ("it repeated something now irrelevant").
    @pytest.mark.parametrize("msg,kind", [
        ("actually forget SaaS - make this a 5-part e-commerce sequence instead", "decision"),
        ("wait, I changed my mind, the product is a mobile fitness app", "decision"),
        ("on second thought, let's target enterprise buyers", "decision"),
        ("switch it to a 2-week drip instead of 3 emails", "decision"),
        ("scrap all that, different approach: focus on retention", "decision"),
        ("rather than email, do it as in-app messages", "decision"),
        ("no longer targeting free users", "decision"),
        ("make the tone much more casual and funny throughout", "preference"),
        ("rewrite every step to be shorter", "preference"),
        # §17.691 — QUESTION-framed pivots (the reported regression).
        ("can't i just erase the old containers and start fresh? i have access and can log in.", "decision"),
        ("why not just do it all over the network?", "decision"),
        ("isn't it easier to clean the existing install?", "decision"),
        ("do I even need the USB step?", "decision"),
        ("couldn't we just reuse the current setup?", "decision"),
        ("why don't we just use the running server?", "decision"),
        ("is there any need to reinstall at all?", "decision"),
        # §17.692 — CURLY apostrophes (U+2019) from smart-quote keyboards must
        # match too — this is the exact class the operator's device produced.
        ("so i already have Proxmox VE downloaded, can’t we just wipe the old containers and start fresh?", "decision"),
        ("why don’t we just do it over the network?", "decision"),
        ("isn’t it easier to clean the existing install?", "decision"),
        ("couldn’t we just reuse it?", "decision"),
    ])
    def test_pivots_detected_with_kind(self, msg, kind):
        assert _vendor._looks_like_pivot(msg) is True
        assert _vendor._pivot_kind(msg) == kind

    def test_normalize_punct_folds_smart_quotes(self):
        assert _vendor._normalize_punct("can’t") == "can't"
        assert _vendor._normalize_punct("“hi”") == '"hi"'
        assert _vendor._normalize_punct("a—b") == "a-b"
        # a curly-apostrophe pivot the raw regex would miss is caught after norm
        assert _vendor._looks_like_pivot("can’t we just skip the reinstall?") is True

    @pytest.mark.parametrize("msg", [
        "what does this step mean?", "can you add a call to action?",
        "make the subject line shorter", "how long should the welcome email be?",
        "explain the activation email", "give me an example",
        "which platform do you recommend?", "",
        # §17.691 — near-misses that must NOT trip the question-pivot patterns.
        "can I add a call to action?",      # "can I" without "just"
        "do I need this for production?",    # "do I need" without "to"
        "why is this step listed here?",     # "why is" is not "why not/don't … just"
        "is there a recommended disk size?", # "is there a …" without need/reason/point to
    ])
    def test_non_pivots_not_detected(self, msg):
        assert _vendor._looks_like_pivot(msg) is False


class TestPivotGateRouting:
    def test_pivot_classified_as_question_routes_to_note_not_rerender(self, pipe):
        """The core §17.679 fix: a pivot the LLM drops to 'question' must go to
        the note→re-plan path, NOT assist_chat_turn (the re-render)."""
        interp = {"intent": "question", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T1"}
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "assist_note_cmd",
                          return_value=iter(["📌 Noted (decision): ..."])) as note_cmd, \
             patch.object(_vendor, "assist_chat_turn",
                          return_value=iter(["RE-RENDER"])) as chat_turn:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID, "actually forget SaaS, make it e-commerce instead",
                node_key="T1"))
        assert "📌 Noted" in out
        note_cmd.assert_called_once()
        assert note_cmd.call_args.kwargs["kind"] == "decision"
        chat_turn.assert_not_called()   # the re-render path was NOT taken

    def test_question_framed_pivot_routes_to_note_not_rerender(self, pipe):
        """§17.691 — the reported regression: a question-framed pivot the LLM
        classified as 'question' must reach the note→re-plan path, not re-render
        the (now-stale) current step."""
        interp = {"intent": "question", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T10",
                  "is_collect": False, "is_decision": False}
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "assist_note_cmd",
                          return_value=iter(["📌 Noted (decision): ..."])) as note_cmd, \
             patch.object(_vendor, "assist_chat_turn",
                          return_value=iter(["RE-RENDER"])) as chat_turn:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID,
                "can't i just erase the old containers and start fresh? i have access and can log in.",
                node_key="T10"))
        assert "📌 Noted" in out
        note_cmd.assert_called_once()
        assert note_cmd.call_args.kwargs["kind"] == "decision"
        chat_turn.assert_not_called()

    def test_curly_apostrophe_pivot_routes_to_note(self, pipe):
        """§17.692 — the reported regression: a curly-apostrophe pivot (from a
        smart-quote keyboard) must route to note→re-plan, not re-render."""
        interp = {"intent": "question", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T6",
                  "is_collect": False, "is_decision": False}
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "assist_note_cmd",
                          return_value=iter(["📌 Noted (decision): ..."])) as note_cmd, \
             patch.object(_vendor, "assist_chat_turn",
                          return_value=iter(["RE-RENDER"])) as chat_turn:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID,
                "so i already have Proxmox VE downloaded, can’t we just wipe the old containers and start fresh?",
                node_key="T6"))
        assert "📌 Noted" in out
        note_cmd.assert_called_once()
        chat_turn.assert_not_called()

    def test_collect_step_refinement_feeds_deliberation_not_rerender(self, pipe):
        """§17.692 — on a collect step, a non-pivot refinement the classifier read
        as `question` ("can we make the port random?") must feed the deliberation
        (submit), not re-render the step and lose the accumulated proposal."""
        interp = {"intent": "question", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T3",
                  "is_collect": True, "is_decision": True}
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "_recall_node_key", return_value="T3"), \
             patch.object(_vendor, "assist_submit",
                          return_value=iter(["DELIBERATION"])) as submit, \
             patch.object(_vendor, "assist_chat_turn",
                          return_value=iter(["RE-RENDER"])) as chat_turn:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID, "can we make the SSH port somewhat random?", node_key="T3"))
        assert "DELIBERATION" in out
        submit.assert_called_once()
        chat_turn.assert_not_called()

    def test_semantic_pivot_skip_surfaces_replan_not_skip(self, pipe):
        """§17.693 — the reported [33] failure: a substantive turn the classifier
        read as `skip` that references the operator's real situation ("i already
        have proxmox installed… we only need to remove old containers") must be
        caught by the impact check and surface a re-plan, NOT silently skip."""
        interp = {"intent": "skip", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T3",
                  "is_collect": False, "is_decision": False}
        affected = [{"node_key": "T3", "action": "drop",
                     "current_assumption": "download the ISO",
                     "proposed_change": "already installed — no ISO needed"}]
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "reroute_check", return_value=affected) as reroute, \
             patch.object(_vendor, "assist_skip",
                          return_value=iter(["SKIPPED"])) as skip:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID,
                "i already have proxmox VE installed and web access, we only need to remove old containers and start new",
                node_key="T3"))
        assert "changes the plan" in out
        assert "Apply these plan changes?" in out
        reroute.assert_called_once()
        skip.assert_not_called()

    def test_skip_without_impact_still_skips(self, pipe):
        """A substantive skip that the impact check clears (no affected steps)
        proceeds as a normal skip — no false re-plan."""
        interp = {"intent": "skip", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T3",
                  "is_collect": False, "is_decision": False}
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "reroute_check", return_value=None) as reroute, \
             patch.object(_vendor, "_recall_node_key", return_value="T3"), \
             patch.object(_vendor, "assist_skip",
                          return_value=iter(["SKIPPED"])) as skip:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID, "skip this one, i handled it manually earlier today",
                node_key="T3"))
        assert "SKIPPED" in out
        reroute.assert_called_once()
        skip.assert_called_once()

    def test_short_skip_does_not_trigger_impact_check(self, pipe):
        """A bare short skip stays below the word bar — no impact LLM call."""
        interp = {"intent": "skip", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T3",
                  "is_collect": False, "is_decision": False}
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "reroute_check", return_value=None) as reroute, \
             patch.object(_vendor, "_recall_node_key", return_value="T3"), \
             patch.object(_vendor, "assist_skip",
                          return_value=iter(["SKIPPED"])) as skip:
            out = "".join(_vendor.assist_nl_turn(pipe, _SID, "skip it", node_key="T3"))
        assert "SKIPPED" in out
        reroute.assert_not_called()   # below assist_pivot_min_words
        skip.assert_called_once()

    def test_genuine_question_still_re_renders(self, pipe):
        """A non-pivot question must still get the current step's guidance."""
        interp = {"intent": "question", "note_text": "", "note_kind": "note",
                  "evidence": "", "error_text": "", "query": "", "node_key": "T1"}
        # §17.934 — `_track_progress` POSTs to /assist/{sid}/track. Unstubbed
        # this test issued a real WRITE against a live session id.
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "fetch_pending_replan", return_value=None), \
             patch.object(_vendor, "_track_progress", return_value=None), \
             patch.object(_vendor, "assist_note_cmd",
                          return_value=iter(["NOTE"])) as note_cmd, \
             patch.object(_vendor, "assist_chat_turn",
                          return_value=iter(["step guidance"])) as chat_turn:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID, "what does this step mean?", node_key="T1"))
        assert "step guidance" in out
        chat_turn.assert_called_once()
        note_cmd.assert_not_called()


class TestConfirmRender:
    def test_apply_summary_lists_revised_and_dropped(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(
            200, {"applied": True, "revised": ["T1"], "dropped": ["T10"]},
        )
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_replan_confirm(pipe, _SID, "apply"))
        assert "✅ Plan updated." in out
        assert "T1" in out and "T10" in out

    def test_discard_summary(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, {"applied": False, "discarded": True})
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_replan_confirm(pipe, _SID, "discard"))
        assert "unchanged" in out.lower()
