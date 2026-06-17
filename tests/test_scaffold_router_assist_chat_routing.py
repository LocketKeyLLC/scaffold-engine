"""§17.537 — assist-aware chat routing.

When a chat has an ACTIVE assist session, plain (non-command) text is a
conversational turn ON that session — routed to the current step's guidance
(`refine=<text>`) instead of the triage planner. Without this, every bare
message mid-assist bounced to triage, freezing the session at its first step
and re-emitting the Scope/Options/Gaps planner blocks. That was the real
DeFruscio HomeLab symptom: the user typed `/assist <job_id>` correctly (session
went `active`, step T1 `presented`) but then asked ~25 plain-language questions,
every one of which routed to triage while the session sat untouched at T1.

Pins:
  * `_active_assist_session` gates on `status == 'active'` AND the valve;
    paused / terminal / missing sessions return None.
  * `pipe()` routes plain text to guidance (not triage, not the §17.504 nudge)
    when a session is active, passing `refine=<msg>` + the recalled node_key.
  * No active session / paused / terminal → triage, unchanged.
  * Slash commands still dispatch regardless of an active session.
  * End-to-end: the delegate renders the orienting banner + the step guidance.
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod

CHAT_BODY = {"metadata": {"chat_id": "chat-1"}}


@pytest.fixture
def pipe():
    return Pipeline()


def _multiturn(last: str) -> list[dict]:
    """A non-first-turn history so the welcome preamble doesn't fire."""
    return [
        {"role": "user", "content": "set up a homelab firewall"},
        {"role": "assistant", "content": "🤝 Assist session started — step T1"},
        {"role": "user", "content": last},
    ]


# ---------------------------------------------------------------------------
# _active_assist_session — the routing gate
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestActiveAssistSessionGate:
    @pytest.mark.parametrize("status,is_active", [
        ("active", True),
        ("paused", False),
        ("completed", False),
        ("abandoned", False),
        ("cancelled", False),
        (None, False),
    ])
    def test_status_gating(self, pipe, status, is_active):
        rec = {"session_id": "s1", "last_node_key": "T1", "status": status}
        with patch.object(pipe, "_assist_recall", return_value=rec):
            result = pipe._active_assist_session("chat-1")
        assert (result is not None) == is_active
        if is_active:
            assert result["session_id"] == "s1"

    def test_recall_miss_returns_none(self, pipe):
        with patch.object(pipe, "_assist_recall", return_value=None):
            assert pipe._active_assist_session("chat-1") is None

    def test_missing_session_id_returns_none(self, pipe):
        with patch.object(pipe, "_assist_recall", return_value={"status": "active"}):
            assert pipe._active_assist_session("chat-1") is None

    def test_valve_off_short_circuits_before_recall(self, pipe):
        pipe.valves.assist_chat_routing_enabled = False
        with patch.object(pipe, "_assist_recall") as recall:
            assert pipe._active_assist_session("chat-1") is None
        recall.assert_not_called()


# ---------------------------------------------------------------------------
# pipe() routing
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestPipeRouting:
    def _drive(self, pipe, msg, *, recall, body=CHAT_BODY):
        with patch.object(pipe, "_assist_recall", return_value=recall), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT") as triage, \
             patch.object(
                 pipe, "_assist_chat_turn",
                 side_effect=lambda *a, **k: iter(["GUIDE_OUTPUT"]),
             ) as guide:
            out = "".join(pipe.pipe(msg, "model-id", _multiturn(msg), body))
        return out, triage, guide

    def test_active_session_routes_to_guidance_not_triage(self, pipe):
        rec = {"session_id": "s1", "last_node_key": "T1", "status": "active"}
        out, triage, guide = self._drive(pipe, "no link up detected", recall=rec)
        assert "GUIDE_OUTPUT" in out
        assert "TRIAGE_OUTPUT" not in out
        triage.assert_not_called()
        guide.assert_called_once()
        args, kwargs = guide.call_args
        assert args[0] == "s1"                     # session_id
        assert args[1] == "no link up detected"    # refine = user text
        assert kwargs["node_key"] == "T1"          # recalled step
        assert kwargs["chat_id"] == "chat-1"

    def test_assist_intent_in_active_session_skips_nudge(self, pipe):
        # §17.504 nudge points at `/assist <job_id>` — wrong once already in a
        # session. Active routing returns before the nudge can fire.
        rec = {"session_id": "s1", "last_node_key": "T1", "status": "active"}
        out, triage, guide = self._drive(
            pipe, "help me implement the firewall step", recall=rec,
        )
        assert pipe._ASSIST_NUDGE not in out
        assert "GUIDE_OUTPUT" in out
        guide.assert_called_once()

    def test_no_active_session_falls_to_triage(self, pipe):
        out, triage, guide = self._drive(
            pipe, "how do I set up the firewall", recall=None,
        )
        assert "TRIAGE_OUTPUT" in out
        triage.assert_called_once()
        guide.assert_not_called()

    def test_paused_session_falls_to_triage(self, pipe):
        rec = {"session_id": "s1", "status": "paused"}
        out, triage, guide = self._drive(
            pipe, "what next for the firewall", recall=rec,
        )
        assert "TRIAGE_OUTPUT" in out
        guide.assert_not_called()

    def test_slash_command_dispatches_despite_active_session(self, pipe):
        # An active session must NOT swallow `/jobs` etc. — slash dispatch
        # happens before the plain-text routing block.
        rec = {"session_id": "s1", "last_node_key": "T1", "status": "active"}
        with patch.object(pipe, "_assist_recall", return_value=rec), \
             patch.object(pipe, "_assist_chat_turn") as guide, \
             patch.object(pipe, "_handle_command", return_value="JOBS_OUTPUT"):
            out = "".join(pipe.pipe("/jobs", "model-id", _multiturn("/jobs"), CHAT_BODY))
        assert "JOBS_OUTPUT" in out
        guide.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end delegate: banner + rendered step guidance
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestAssistChatTurnEndToEnd:
    def test_banner_then_guidance_rendered(self, pipe):
        pipe.valves.assist_stream = False  # exercise the blocking guide path
        guidance_payload = {
            "node_key": "T1",
            "status": "ready",
            "guidance": "Boot the OPNsense installer USB and select the WAN port.",
        }
        rec = {"session_id": "s1", "last_node_key": "T1", "status": "active"}
        with patch.object(pipe, "_assist_recall", return_value=rec), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT"), \
             patch.object(
                 _mod._HTTP_SESSION, "post",
                 return_value=_make_response(200, guidance_payload),
             ):
            out = "".join(
                pipe.pipe("no link up detected", "model-id",
                          _multiturn("no link up detected"), CHAT_BODY)
            )
        assert "TRIAGE_OUTPUT" not in out
        assert "active assist session" in out          # orienting banner
        assert "/assist next" in out                    # advance hint
        assert "How to do this step" in out             # rendered guidance
        assert "Boot the OPNsense installer USB" in out
