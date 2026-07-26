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
        # §17.626 — active-session plain text now enters via `_assist_nl_turn`
        # (classify-and-route), which supersedes the §17.537 always-guide method.
        # Same signature, so the call_args assertions below are unchanged.
        # §17.633 — isolate the session-routing gate under test: stub the
        # cross-chat reconnection + in-progress banner (both make live
        # /assist/candidates calls) so these assertions don't depend on what
        # in-progress jobs happen to exist in the test orchestrator.
        with patch.object(pipe, "_assist_recall", return_value=recall), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT") as triage, \
             patch.object(pipe, "_reconnect_in_progress", return_value=None), \
             patch.object(pipe, "_in_progress_banner", return_value=""), \
             patch.object(
                 pipe, "_assist_nl_turn",
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

    def test_active_session_beats_stale_pick_list(self, pipe):
        # §17.652 — a spent `<!--ASSIST_PICK-->` stays in history forever. Once a
        # session is active, a later substantive message that merely contains a
        # candidate job's distinctive token ("…host the WireGuard endpoint…") must
        # route to the SESSION, not re-resolve the pick and re-present the step.
        rec = {"session_id": "s1", "last_node_key": "T1", "status": "active"}
        msgs = [
            {"role": "user", "content": "continue the proxmox setup"},
            {"role": "assistant",
             "content": "which job? <!--ASSIST_PICK:aaaa1111,bbbb2222-->"},
            {"role": "user", "content": "the first one"},
            {"role": "assistant", "content": "🤝 Assist session started — step T1"},
            {"role": "user",
             "content": "which computer should host the WireGuard endpoint?"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=rec), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT"), \
             patch.object(pipe, "_reconnect_in_progress", return_value=None), \
             patch.object(pipe, "_in_progress_banner", return_value=""), \
             patch.object(_mod._assist, "resolve_candidate_pick") as pick, \
             patch.object(_mod._assist, "assist_start") as start, \
             patch.object(
                 pipe, "_assist_nl_turn",
                 side_effect=lambda *a, **k: iter(["GUIDE_OUTPUT"]),
             ) as guide:
            out = "".join(pipe.pipe(msgs[-1]["content"], "model-id", msgs, CHAT_BODY))
        assert "GUIDE_OUTPUT" in out
        guide.assert_called_once()
        pick.assert_not_called()   # pick block skipped entirely
        start.assert_not_called()  # session NOT re-started

    def test_pick_resolves_when_no_active_session(self, pipe):
        # Guard the normal flow: with NO active session, a selector reply against
        # a pending pick-list still starts the chosen job (§17.652 didn't break it).
        msgs = [
            {"role": "user", "content": "continue the proxmox setup"},
            {"role": "assistant",
             "content": "which job? <!--ASSIST_PICK:aaaa1111,bbbb2222-->"},
            {"role": "user", "content": "the first one"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_active_assist_session_via_history", return_value=None), \
             patch.object(pipe, "_reconnect_in_progress", return_value=None), \
             patch.object(pipe, "_in_progress_banner", return_value=""), \
             patch.object(_mod._assist, "resolve_candidate_pick", return_value="aaaa1111") as pick, \
             patch.object(
                 _mod._assist, "assist_start",
                 side_effect=lambda *a, **k: iter(["STARTED aaaa1111"]),
             ) as start:
            out = "".join(pipe.pipe(msgs[-1]["content"], "model-id", msgs, CHAT_BODY))
        assert "STARTED aaaa1111" in out
        pick.assert_called_once()
        start.assert_called_once()

    def test_slash_command_dispatches_despite_active_session(self, pipe):
        # An active session must NOT swallow `/jobs` etc. — slash dispatch
        # happens before the plain-text routing block. §17.562: /jobs is an
        # advanced command, so enable advanced mode for this dispatch check.
        pipe.valves.advanced_commands_enabled = True
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


# ---------------------------------------------------------------------------
# §17.539 — history-based recovery (chat_id is unavailable in this OWUI setup)
# ---------------------------------------------------------------------------

# The real assist-start marker as OWUI saves it (from the live chat DB dump).
_START_MARKER = (
    "🤝 **Assist session started** — `50815e37-76c7-4862-90e3-354d481f7c3b`\n\n"
    "Job `915fa635-eea0-4b0e-a90b-8a512ceb3b9b` is now in `assisted_executing` "
    "(9 pending steps)."
)
# Body with NO chat_id — the confirmed production reality (OWUI pops metadata).
NO_CID_BODY: dict = {}


@pytest.mark.smoke
class TestSessionIdFromHistory:
    def test_extracts_from_real_marker(self, pipe):
        msgs = [
            {"role": "user", "content": "/assist 915fa635-…"},
            {"role": "assistant", "content": _START_MARKER},
            {"role": "user", "content": "no link up detected"},
        ]
        assert pipe._session_id_from_history(msgs) == \
            "50815e37-76c7-4862-90e3-354d481f7c3b"

    def test_returns_most_recent_when_multiple(self, pipe):
        older = _START_MARKER  # session 50815e37…
        newer = "🤝 **Assist session started** — `aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee`"
        msgs = [
            {"role": "assistant", "content": older},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": newer},
            {"role": "user", "content": "what next"},
        ]
        assert pipe._session_id_from_history(msgs) == \
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_none_when_no_marker(self, pipe):
        msgs = [
            {"role": "user", "content": "build me a homelab"},
            {"role": "assistant", "content": "**Scope so far:** ..."},
        ]
        assert pipe._session_id_from_history(msgs) is None

    def test_ignores_marker_in_user_turn(self, pipe):
        # Only assistant turns are authoritative (a user could paste anything).
        msgs = [{"role": "user", "content": _START_MARKER}]
        assert pipe._session_id_from_history(msgs) is None


@pytest.mark.smoke
class TestActiveAssistSessionViaHistory:
    def _msgs(self):
        return [
            {"role": "assistant", "content": _START_MARKER},
            {"role": "user", "content": "no link up detected"},
        ]

    def test_active_session_recovered(self, pipe):
        sess = {"status": "active", "current_node_key": "T1"}
        with patch.object(pipe, "_get_assist_session", return_value=sess):
            out = pipe._active_assist_session_via_history(self._msgs())
        assert out == {
            "session_id": "50815e37-76c7-4862-90e3-354d481f7c3b",
            "last_node_key": "T1",
            "status": "active",
        }

    def test_terminal_session_not_recovered(self, pipe):
        with patch.object(pipe, "_get_assist_session",
                          return_value={"status": "completed"}):
            assert pipe._active_assist_session_via_history(self._msgs()) is None

    def test_unreachable_session_not_recovered(self, pipe):
        with patch.object(pipe, "_get_assist_session", return_value=None):
            assert pipe._active_assist_session_via_history(self._msgs()) is None

    def test_no_marker_skips_http(self, pipe):
        with patch.object(pipe, "_get_assist_session") as get:
            assert pipe._active_assist_session_via_history(
                [{"role": "user", "content": "hi"}]) is None
        get.assert_not_called()


@pytest.mark.smoke
class TestPipeRoutesViaHistoryWithoutChatId:
    def test_no_chat_id_still_routes_via_history(self, pipe):
        # The actual production failure: body has NO chat_id, but the active
        # session is recoverable from the assist-start marker in history.
        msgs = [
            {"role": "assistant", "content": _START_MARKER},
            {"role": "user", "content": "what's my next step"},
        ]
        sess = {"status": "active", "current_node_key": "T1"}
        with patch.object(pipe, "_get_assist_session", return_value=sess), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT") as triage, \
             patch.object(
                 pipe, "_assist_nl_turn",  # §17.626 — new active-session entry point
                 side_effect=lambda *a, **k: iter(["GUIDE_OUTPUT"]),
             ) as guide:
            out = "".join(pipe.pipe("what's my next step", "model-id", msgs, NO_CID_BODY))
        assert "GUIDE_OUTPUT" in out
        assert "TRIAGE_OUTPUT" not in out
        triage.assert_not_called()
        args, kwargs = guide.call_args
        assert args[0] == "50815e37-76c7-4862-90e3-354d481f7c3b"
        assert args[1] == "what's my next step"
        assert kwargs["node_key"] == "T1"

    def test_no_chat_id_no_history_marker_falls_to_triage(self, pipe):
        msgs = [
            {"role": "user", "content": "build a homelab"},
            {"role": "assistant", "content": "**Scope so far:** ..."},
            {"role": "user", "content": "what about networking"},
        ]
        with patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT") as triage, \
             patch.object(pipe, "_assist_nl_turn") as guide:
            out = "".join(pipe.pipe("what about networking", "model-id", msgs, NO_CID_BODY))
        assert "TRIAGE_OUTPUT" in out
        guide.assert_not_called()


@pytest.mark.smoke
class TestWorkFallbackResume:
    """§17.646 — when OWUI sends no chat_id AND truncates the assist-start marker
    out of a long transcript, a plain "done"/"next" must still resume the sole
    active session (DB-derived via /work), so completion is recorded and the step
    advances instead of bouncing to the planner."""

    @pytest.mark.parametrize("msg", [
        "done", "done, I created the LXC container", "next", "what's next",
        "I installed pi-hole", "it failed with an error", "skip this",
        "I've finished the step", "status",
    ])
    def test_continuation_phrases_detected(self, pipe, msg):
        assert pipe._looks_like_assist_continuation(msg) is True

    @pytest.mark.parametrize("msg", [
        "build me a web scraper in python",
        "set up a hardened homelab on a Supermicro server",
        "what about networking best practices",
        "create a budget tracker app",
    ])
    def test_new_ideas_not_treated_as_continuation(self, pipe, msg):
        assert pipe._looks_like_assist_continuation(msg) is False

    def _work(self, sessions):
        return {"jobs": [], "assist_sessions": sessions}

    def test_sole_active_session_returned(self, pipe):
        sessions = [{"session_id": "s-1", "status": "active", "current_node_key": "T3"}]
        with patch.object(pipe, "_fetch_work", return_value=self._work(sessions)):
            got = pipe._sole_active_session_via_work()
        assert got == {"session_id": "s-1", "last_node_key": "T3", "status": "active"}

    def test_zero_active_returns_none(self, pipe):
        with patch.object(pipe, "_fetch_work", return_value=self._work([])):
            assert pipe._sole_active_session_via_work() is None

    def test_multiple_active_returns_none(self, pipe):
        sessions = [
            {"session_id": "s-1", "status": "active", "current_node_key": "T1"},
            {"session_id": "s-2", "status": "active", "current_node_key": "T2"},
        ]
        with patch.object(pipe, "_fetch_work", return_value=self._work(sessions)):
            assert pipe._sole_active_session_via_work() is None

    def test_continuation_no_marker_resumes_sole_session(self, pipe):
        """The fix: no chat_id, no marker in history, a "done" turn — resume the
        one active session via /work and route it through _assist_nl_turn."""
        msgs = [
            {"role": "user", "content": "earlier stuff"},
            {"role": "assistant", "content": "some walkthrough with no session marker"},
            {"role": "user", "content": "done, I created the LXC"},
        ]
        sessions = [{"session_id": "s-9", "status": "active", "current_node_key": "T2"}]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_fetch_work", return_value=self._work(sessions)), \
             patch.object(pipe, "_reconnect_in_progress", return_value=None), \
             patch.object(pipe, "_in_progress_banner", return_value=""), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT") as triage, \
             patch.object(pipe, "_assist_nl_turn",
                          side_effect=lambda *a, **k: iter(["GUIDE_OUTPUT"])) as guide:
            out = "".join(pipe.pipe("done, I created the LXC", "model-id", msgs, NO_CID_BODY))
        assert "GUIDE_OUTPUT" in out
        assert "TRIAGE_OUTPUT" not in out
        triage.assert_not_called()
        args, kwargs = guide.call_args
        assert args[0] == "s-9"
        assert kwargs["node_key"] == "T2"

    def test_new_idea_no_marker_does_not_hijack_sole_session(self, pipe):
        """A genuinely new idea (not a continuation) must NOT be swallowed by the
        active session — it goes to the planner, and /work is never consulted."""
        msgs = [
            {"role": "user", "content": "build a hardened homelab on a Supermicro server"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_fetch_work") as work, \
             patch.object(pipe, "_reconnect_in_progress", return_value=None), \
             patch.object(pipe, "_in_progress_banner", return_value=""), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT") as triage, \
             patch.object(pipe, "_assist_nl_turn") as guide:
            out = "".join(pipe.pipe(
                "build a hardened homelab on a Supermicro server", "model-id", msgs, NO_CID_BODY))
        assert "TRIAGE_OUTPUT" in out
        guide.assert_not_called()
        work.assert_not_called()  # gated out before the /work call
