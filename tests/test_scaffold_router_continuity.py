"""§17.633 — cross-chat assist continuity.

OWUI sends no chat_id and a NEW chat has no session marker in history, so
neither the chatmap nor the history-recovery path can find in-progress assist
work started in another chat (the repeatedly-reported "natural language isn't
prevalent in a new chat" symptom). This adds:

  * `_reconnect_in_progress` — a plain natural message that names an in-progress
    job ("continue the proxmox setup") or reads as resuming ("what's next",
    "where were we") reconnects to it (assist_start is idempotent → re-presents
    the current step + re-emits the marker so THIS chat tracks it).
  * `_in_progress_banner` — a NEW chat's first turn surfaces in-progress work.

Pins the routing: unique/resume → resume, ambiguous/topic-less → pick-list,
genuine new idea → None (planner untouched). Run with --noconftest.
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline, _mod

# Three in-progress jobs (1 active, 2 awaiting_assist) — the shape
# /assist/candidates returns.
CANDS = [
    {"job_id": "11111111-1111-1111-1111-111111111111",
     "title": "Proxmox VE Installation on Dual Xeon", "status": "assisted_executing"},
    {"job_id": "22222222-2222-2222-2222-222222222222",
     "title": "Firewall and VPN Gateway Setup", "status": "awaiting_assist"},
    {"job_id": "33333333-3333-3333-3333-333333333333",
     "title": "Secure Remote Access for Home Lab", "status": "awaiting_assist"},
]


@pytest.fixture
def pipe():
    return Pipeline()


def _mock_assist(candidates=CANDS):
    """Patch the vendor helpers _reconnect_in_progress leans on: candidate fetch
    (deterministic set) + assist_start (yields a job-id sentinel to assert on).
    match_assist_candidate / render_candidate_list stay REAL (pure functions)."""
    return (
        patch.object(_mod._assist, "fetch_assist_candidates", return_value=candidates),
        patch.object(_mod._assist, "assist_start",
                     side_effect=lambda p, jid, **k: iter([f"RESUMED:{jid}"])),
    )


# ---------------------------------------------------------------------------
# _looks_like_resume
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestLooksLikeResume:
    @pytest.mark.parametrize("msg", [
        "let's continue setting up proxmox", "what's next", "whats next",
        "where were we", "keep going", "pick up where we left off",
        "resume", "carry on", "let's finish the firewall", "back to the homelab",
    ])
    def test_positive(self, pipe, msg):
        assert pipe._looks_like_resume(msg) is True

    @pytest.mark.parametrize("msg", [
        "build me a screenshot to PDF tool", "what is a DAG",
        "set up a brand new firewall from scratch", "how are you",
    ])
    def test_negative(self, pipe, msg):
        assert pipe._looks_like_resume(msg) is False


# ---------------------------------------------------------------------------
# _reconnect_in_progress
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestReconnect:
    def test_strong_unique_topic_match_resumes(self, pipe):
        # ≥2 distinctive shared tokens (proxmox, installation) → resume now.
        fc, as_ = _mock_assist()
        with fc, as_:
            out = "".join(pipe._reconnect_in_progress(
                "continue the proxmox installation", chat_id=None))
        assert out == "RESUMED:11111111-1111-1111-1111-111111111111"

    def test_single_topic_token_plus_resume_resumes(self, pipe):
        # "proxmox" is 1 distinctive token — the default bar treats it ambiguous,
        # but a resume phrasing lowers it to 1 and it's unique → resume.
        fc, as_ = _mock_assist()
        with fc, as_:
            out = "".join(pipe._reconnect_in_progress(
                "let's continue proxmox", chat_id=None))
        assert out == "RESUMED:11111111-1111-1111-1111-111111111111"

    def test_resume_phrase_no_topic_multiple_shows_picklist(self, pipe):
        fc, as_ = _mock_assist()
        with fc, as_:
            out = "".join(pipe._reconnect_in_progress("where were we", chat_id=None))
        assert "which job" in out.lower() or "ASSIST_PICK" in out
        assert "RESUMED:" not in out

    def test_resume_phrase_single_candidate_resumes(self, pipe):
        fc, as_ = _mock_assist(candidates=[CANDS[0]])
        with fc, as_:
            out = "".join(pipe._reconnect_in_progress("what's next", chat_id=None))
        assert out == "RESUMED:11111111-1111-1111-1111-111111111111"

    def test_new_idea_no_reconnect(self, pipe):
        fc, as_ = _mock_assist()
        with fc, as_:
            r = pipe._reconnect_in_progress("build me a screenshot to PDF tool",
                                            chat_id=None)
        assert r is None

    def test_no_candidates_returns_none(self, pipe):
        fc, as_ = _mock_assist(candidates=[])
        with fc, as_:
            assert pipe._reconnect_in_progress("continue proxmox", chat_id=None) is None

    def test_weak_signal_never_auto_resumes(self, pipe):
        # A weak, non-resume topic touch ("the setup" shares 1 generic token)
        # must NOT auto-resume a job — either a pick-list or no-reconnect
        # (None → falls through to the planner) is acceptable.
        fc, as_ = _mock_assist()
        with fc, as_:
            r = pipe._reconnect_in_progress("the setup", chat_id=None)
        out = "".join(r) if r is not None else ""
        assert "RESUMED:" not in out


# ---------------------------------------------------------------------------
# _in_progress_banner
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestBanner:
    def test_renders_with_candidates(self, pipe):
        with patch.object(_mod._assist, "fetch_assist_candidates", return_value=CANDS):
            b = pipe._in_progress_banner()
        assert "3 task(s) in progress" in b
        assert "Proxmox" in b and "Firewall" in b
        assert "continue" in b.lower()

    def test_empty_without_candidates(self, pipe):
        with patch.object(_mod._assist, "fetch_assist_candidates", return_value=[]):
            assert pipe._in_progress_banner() == ""


# ---------------------------------------------------------------------------
# pipe() end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestPipeContinuity:
    def _fresh(self, msg):
        return [{"role": "user", "content": msg}]  # new chat: one message, no marker

    def test_new_chat_resume_reconnects_not_triage(self, pipe):
        fc, as_ = _mock_assist()
        with fc, as_, \
             patch.object(pipe, "_active_assist_session", return_value=None), \
             patch.object(pipe, "_active_assist_session_via_history", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage:
            out = "".join(pipe.pipe("let's continue proxmox", "m",
                                    self._fresh("let's continue proxmox"), {}))
        assert "RESUMED:11111111-1111-1111-1111-111111111111" in out
        assert "TRIAGE" not in out
        triage.assert_not_called()

    def test_new_idea_first_turn_shows_banner_then_triage(self, pipe):
        fc, as_ = _mock_assist()
        with fc, as_, \
             patch.object(pipe, "_active_assist_session", return_value=None), \
             patch.object(pipe, "_active_assist_session_via_history", return_value=None), \
             patch.object(pipe, "_classify_command",
                          return_value={"intent": "none", "confidence": "low"}), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage:
            out = "".join(pipe.pipe("build me a screenshot to PDF tool", "m",
                                    self._fresh("build me a screenshot to PDF tool"), {}))
        assert "in progress" in out            # banner surfaced
        assert "TRIAGE" in out                 # planner still ran
        triage.assert_called_once()

    # The REAL OWUI title-generation prompt shape (open_webui/config.py
    # DEFAULT_TITLE_GENERATION_PROMPT_TEMPLATE) — starts with "### Task:" and
    # carries a "### Chat History:" block. OWUI sends THIS as the message and
    # does NOT forward body.metadata.task to an external pipeline.
    _REAL_TASK_PROMPT = (
        "### Task:\nGenerate a concise, 3-5 word title with an emoji "
        "summarizing the chat history.\n### Guidelines:\n- ...\n### Output:\n"
        'JSON format: { "title": "..." }\n### Chat History:\n<chat_history>\n'
        "USER: let's continue setting up proxmox\n</chat_history>"
    )

    def test_is_owui_task_call_detector(self, pipe):
        # Content signal (the load-bearing path — metadata.task is NOT forwarded).
        assert pipe._is_owui_task_call({}, self._REAL_TASK_PROMPT) is True
        # metadata.task belt-and-suspenders (clients that DO forward it).
        assert pipe._is_owui_task_call({"metadata": {"task": "title_generation"}}, "hi") is True
        # Real user turns must NOT be detected as task calls.
        assert pipe._is_owui_task_call({}, "let's continue setting up proxmox") is False
        assert pipe._is_owui_task_call({}, "### Task: my own heading, no chat history") is False

    def test_owui_task_call_short_circuits_no_side_effects(self, pipe):
        # The real content-based path: a "### Task:…### Chat History:" prompt must
        # bypass ALL routing — the continuity path calls assist_start (a real
        # side effect) that spuriously started sessions in the live browser test.
        with patch.object(pipe, "_direct_completion", return_value="📉 Title") as dc, \
             patch.object(pipe, "_reconnect_in_progress") as rec, \
             patch.object(pipe, "_call_triage") as triage, \
             patch.object(pipe, "_nl_command_route") as nl:
            out = "".join(pipe.pipe(
                self._REAL_TASK_PROMPT, "m",
                [{"role": "user", "content": self._REAL_TASK_PROMPT}], {},
            ))
        assert out == "📉 Title"
        dc.assert_called_once()
        rec.assert_not_called()
        triage.assert_not_called()
        nl.assert_not_called()

    def test_no_task_marker_routes_normally(self, pipe):
        # A real user turn (no task marker) must NOT hit _direct_completion.
        with patch.object(pipe, "_direct_completion") as dc, \
             patch.object(pipe, "_active_assist_session", return_value=None), \
             patch.object(pipe, "_active_assist_session_via_history", return_value=None), \
             patch.object(pipe, "_reconnect_in_progress", return_value=None), \
             patch.object(pipe, "_in_progress_banner", return_value=""), \
             patch.object(pipe, "_classify_command",
                          return_value={"intent": "none", "confidence": "low"}), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE"):
            out = "".join(pipe.pipe("build a thing", "m",
                                    [{"role": "user", "content": "build a thing"}], {}))
        assert "TRIAGE" in out
        dc.assert_not_called()

    def test_valve_off_disables_continuity(self, pipe):
        pipe.valves.assist_continuity_enabled = False
        with patch.object(_mod._assist, "fetch_assist_candidates") as fc, \
             patch.object(pipe, "_active_assist_session", return_value=None), \
             patch.object(pipe, "_active_assist_session_via_history", return_value=None), \
             patch.object(pipe, "_classify_command",
                          return_value={"intent": "none", "confidence": "low"}), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE"):
            out = "".join(pipe.pipe("continue proxmox", "m",
                                    self._fresh("continue proxmox"), {}))
        assert "TRIAGE" in out
        fc.assert_not_called()                 # no candidate fetch when off
