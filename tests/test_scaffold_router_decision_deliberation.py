"""§17.689 — pipeline side of decision deliberation.

- assist_submit renders a `deliberating` reply (proposal, no commit) and keeps
  the step open; a `committed` decision leads with what was recorded.
- assist_submit forwards the conversation `history` so the server can assemble
  the artifact across turns.
- _looks_like_decision_confirm is the deterministic backstop that routes a
  "looks good" reply to submit on a decision node.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipe():
    return Pipeline()


class TestDeliberatingRender:
    def test_deliberating_shows_proposal_and_does_not_commit(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, {
            "status": "deliberating",
            "node_key": "T2",
            "committed": False,
            "decision_message": "Here's a concrete 3-VLAN table: VLAN 10 mgmt …",
        })
        with patch.object(_vendor, "_ss", return_value=sess), \
             patch.object(_vendor, "assist_remember"):
            out = "".join(_vendor.assist_submit(
                pipe, _SID, "T2", "3 vlans", chat_id="c1",
                history=[{"role": "assistant", "content": "How many VLANs?"}],
            ))
        assert "3-VLAN table" in out
        assert "confirm" in out.lower()
        # NOT presented as a committed/advanced step
        assert "committed" not in out.lower()
        assert "Moving on" not in out
        # history was forwarded for cross-turn assembly
        _, kwargs = sess.post.call_args
        assert kwargs["json"]["history"] == [{"role": "assistant", "content": "How many VLANs?"}]

    def test_resolved_commit_leads_with_recorded_decision(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, {
            "status": "committed",
            "node_key": "T2",
            "committed": True,
            "next_node_key": "T10",
            "decision_message": "Locked in a 3-VLAN plan (mgmt/trusted/guest).",
        })
        with patch.object(_vendor, "_ss", return_value=sess), \
             patch.object(_vendor, "assist_remember"), \
             patch.object(_vendor, "assist_next",
                          side_effect=lambda *a, **k: iter(["<next step>"])):
            out = "".join(_vendor.assist_submit(
                pipe, _SID, "T2", "looks good", chat_id="c1",
            ))
        assert "Decision recorded" in out
        assert "3-VLAN plan" in out
        assert "committed" in out.lower()


class TestGatherRender:
    def test_gather_deliberating_invites_remaining_pieces(self, pipe):
        # §17.690 — a gather step that has only part of the requested info shows
        # what's captured + what's missing, and invites the rest a piece at a time.
        sess = MagicMock()
        sess.post.return_value = _make_response(200, {
            "status": "deliberating",
            "node_key": "T2",
            "committed": False,
            "collect_kind": "gather",
            "decision_message": "Captured: disk inventory. Still need: model, GPU(s), NIC models.",
        })
        with patch.object(_vendor, "_ss", return_value=sess), \
             patch.object(_vendor, "assist_remember"):
            out = "".join(_vendor.assist_submit(
                pipe, _SID, "T2", "<lsblk output>", chat_id="c1",
            ))
        assert "Still need" in out
        assert "one piece at a time" in out.lower()
        assert "looks good" not in out.lower()  # not the decision-confirm hint
        assert "committed" not in out.lower()

    def test_gather_resolved_uses_neutral_recorded_label(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, {
            "status": "committed", "node_key": "T2", "committed": True,
            "next_node_key": None, "collect_kind": "gather",
            "decision_message": "All hardware details recorded.",
        })
        with patch.object(_vendor, "_ss", return_value=sess), \
             patch.object(_vendor, "assist_remember"):
            out = "".join(_vendor.assist_submit(
                pipe, _SID, "T2", "no GPU; NIC is Intel X540", chat_id="c1",
            ))
        assert "📌 **Recorded.**" in out
        assert "Decision recorded" not in out


class TestDecisionConfirmBackstop:
    @pytest.mark.parametrize("msg", [
        "looks good", "Looks Good!", "sounds good", "that works", "go with that",
        "perfect", "yes", "yep", "confirm it", "lock it in", "that's the plan",
        "ok", "great",
    ])
    def test_confirmations_match(self, msg):
        assert _vendor._looks_like_decision_confirm(msg)

    @pytest.mark.parametrize("msg", [
        "what's the difference between the options?",
        "how many VLANs do you recommend?",
        "actually make it a DMZ instead",
        "",
    ])
    def test_non_confirmations_do_not_match(self, msg):
        assert not _vendor._looks_like_decision_confirm(msg)
