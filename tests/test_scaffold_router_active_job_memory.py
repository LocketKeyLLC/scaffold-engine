"""§17.307 — active-job chat memory (pilot).

Pre-§17.307 every job-id-taking command required the operator to
paste a 36-char UUID. Operators with one active job per chat (the
common case) paid the UUID-paste tax every turn.

§17.307 pilots in-pipeline `chat_id → job_id` memory:
  - WRITER: `/idea` success seeds the cache via the
    `_render_ideate_response` path
  - READERS: `/results` and `/cost` invoked with NO explicit id
    fall back to the cached id and surface a 📌 hint
  - Explicit args always override (no surprise)
  - Empty cache + no arg = unchanged Usage error from §17.301

The pilot is scoped to /idea + /results + /cost specifically. The
remaining job-id-taking commands (/exec retry, /jobs rename/delete,
/skip, /logs, /execute, /dag, /confirm) are deferred until the
pilot's UX is validated.

These tests pin: writer / reader / explicit-override / cross-chat
isolation / valve gate / hint shape / source-shape regression guards.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"
_SAMPLE_JOB_ID_2 = "ffff1111-2222-3333-4444-555566667777"
_CHAT_A = "chat-aaa-111"
_CHAT_B = "chat-bbb-222"


# ---------------------------------------------------------------------------
# Helper API (remember / recall / hint)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestRememberRecallHelpers:

    def test_remember_then_recall_returns_job_id(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my CLI")
        recalled = pipe._active_job_recall(_CHAT_A)
        assert recalled is not None
        assert recalled["job_id"] == _SAMPLE_JOB_ID
        assert recalled["title"] == "my CLI"

    def test_remember_overwrites_per_chat(self, pipe):
        """Subsequent /idea on the same chat overwrites — no stale id."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID_2)
        assert pipe._active_job_recall(_CHAT_A)["job_id"] == _SAMPLE_JOB_ID_2

    def test_recall_returns_none_when_unset(self, pipe):
        assert pipe._active_job_recall(_CHAT_A) is None

    def test_recall_returns_none_when_chat_id_falsy(self, pipe):
        """Pre-condition for all readers: empty/None chat_id can't
        produce a recall — the caller falls through to Usage error."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        assert pipe._active_job_recall(None) is None
        assert pipe._active_job_recall("") is None

    def test_remember_records_timestamp(self, pipe):
        before = time.time()
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        after = time.time()
        recalled = pipe._active_job_recall(_CHAT_A)
        assert before <= recalled["remembered_at"] <= after

    def test_cross_chat_isolation(self, pipe):
        """Memory is per-chat — two chats with two jobs don't bleed."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        pipe._active_job_remember(_CHAT_B, _SAMPLE_JOB_ID_2)
        assert pipe._active_job_recall(_CHAT_A)["job_id"] == _SAMPLE_JOB_ID
        assert pipe._active_job_recall(_CHAT_B)["job_id"] == _SAMPLE_JOB_ID_2


# ---------------------------------------------------------------------------
# Valve gate
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestValveGate:

    def test_remember_no_op_when_valve_off(self, pipe):
        pipe.valves.active_job_memory_enabled = False
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        assert pipe._active_job_recall(_CHAT_A) is None

    def test_recall_no_op_when_valve_off(self, pipe):
        """Even with cache populated, valve-off path returns None.
        Operators who flipped it off get the pre-§17.307 behavior."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        pipe.valves.active_job_memory_enabled = False
        assert pipe._active_job_recall(_CHAT_A) is None


# ---------------------------------------------------------------------------
# 📌 Hint shape
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestActiveJobHint:

    def test_hint_contains_short_id(self, pipe):
        """The hint shows the 8-char short id (not the full 36-char
        UUID) — the operator-recognizable handle from /jobs output."""
        out = pipe._active_job_hint(_SAMPLE_JOB_ID, None)
        assert "abc1234e" in out
        # Pin literal — don't print the full UUID; it's noise.
        assert _SAMPLE_JOB_ID not in out

    def test_hint_includes_title_when_present(self, pipe):
        out = pipe._active_job_hint(_SAMPLE_JOB_ID, "Build a CLI")
        assert "Build a CLI" in out

    def test_hint_omits_title_dash_when_absent(self, pipe):
        """No double-dash when title is None — clean hint."""
        out = pipe._active_job_hint(_SAMPLE_JOB_ID, None)
        # No ` — ` separator dangling without a title.
        assert " — _" not in out

    def test_hint_carries_override_instruction(self, pipe):
        """The hint always tells operators how to bypass — pin the
        instruction so a "shorter is better" refactor doesn't drop it."""
        out = pipe._active_job_hint(_SAMPLE_JOB_ID, None)
        assert "override" in out.lower()

    def test_hint_ends_with_blank_line(self, pipe):
        """Hint is prepended to body — must end with `\\n\\n` so the
        body starts on a fresh line, not glued to the hint."""
        out = pipe._active_job_hint(_SAMPLE_JOB_ID, None)
        assert out.endswith("\n\n")


# ---------------------------------------------------------------------------
# Writer: /idea success seeds memory
# ---------------------------------------------------------------------------


def _ideate_response(**overrides) -> MagicMock:
    body = {
        "job_id": _SAMPLE_JOB_ID,
        "status": "awaiting_confirmation",
        "refined_brief": {"title": "Build a CLI that converts screenshots to PDF"},
        "feasibility": {"feasible": True, "confidence": 0.85},
    }
    body.update(overrides)
    r = MagicMock(status_code=200, text="")
    r.json.return_value = body
    return r


@pytest.mark.smoke
class TestIdeaSeedsMemory:

    def test_render_ideate_seeds_memory_with_chat_id(self, pipe):
        """The renderer is the writer entry — `/idea` dispatch calls it
        with chat_id from the request body."""
        pipe._render_ideate_response(_ideate_response(), chat_id=_CHAT_A)
        recalled = pipe._active_job_recall(_CHAT_A)
        assert recalled is not None
        assert recalled["job_id"] == _SAMPLE_JOB_ID
        # Title pulled from refined_brief.
        assert recalled["title"] == "Build a CLI that converts screenshots to PDF"

    def test_no_chat_id_no_cache_write(self, pipe):
        """Curl-only callers (no chat_id) don't pollute the cache."""
        pipe._render_ideate_response(_ideate_response(), chat_id=None)
        assert pipe._active_job_recall(_CHAT_A) is None

    def test_failed_response_no_cache_write(self, pipe):
        """4xx / non-JSON / missing job_id paths fall through to _fmt —
        the cache must NOT be polluted with a non-existent id."""
        r = MagicMock(status_code=422, text="")
        r.json.return_value = {"detail": "validation failed"}
        pipe._render_ideate_response(r, chat_id=_CHAT_A)
        assert pipe._active_job_recall(_CHAT_A) is None


# ---------------------------------------------------------------------------
# Reader: /results recalls when no arg
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestResultsReader:

    def test_no_arg_with_memory_uses_cached_id(self, pipe):
        """The recall path produces a 📌 hint + the standard body."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my job")
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(
                status_code=200,
                json=lambda: {"status": "completed", "compiled_output": "DONE"},
            )
            out = pipe._handle_results(["/results"], chat_id=_CHAT_A)
        # Hint prepended.
        assert "📌" in out
        assert "abc1234e" in out
        # Standard body follows.
        assert "DONE" in out
        # Verify the request went to the recalled id.
        called_url = mg.call_args[0][0]
        assert _SAMPLE_JOB_ID in called_url

    def test_no_arg_no_memory_returns_usage(self, pipe):
        """Empty cache + no arg = unchanged Usage error from §17.301.
        Operators with a cold cache don't get a surprise."""
        out = pipe._handle_results(["/results"], chat_id=_CHAT_A)
        assert "Usage:" in out
        assert "/results <job_id>" in out
        # No hint emitted when nothing was recalled.
        assert "📌" not in out

    def test_explicit_id_overrides_memory(self, pipe):
        """When operator passes an explicit id, that's used — memory
        is NOT consulted (no hint, no substitution)."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(
                status_code=200,
                json=lambda: {"status": "completed", "compiled_output": "BODY"},
            )
            out = pipe._handle_results(
                ["/results", _SAMPLE_JOB_ID_2], chat_id=_CHAT_A,
            )
        assert "📌" not in out
        called_url = mg.call_args[0][0]
        # The explicit id is used, NOT the cached one.
        assert _SAMPLE_JOB_ID_2 in called_url
        assert _SAMPLE_JOB_ID not in called_url

    def test_no_chat_id_no_recall(self, pipe):
        """Without a chat_id, recall can't fire even if cache has rows.
        Falls through to Usage error."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = pipe._handle_results(["/results"], chat_id=None)
        assert "Usage:" in out
        assert "📌" not in out


# ---------------------------------------------------------------------------
# Reader: /cost recalls when no arg
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestCostReader:

    def test_no_arg_with_memory_uses_cached_id(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my job")
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "total_cost_usd": 0.12, "call_count": 5,
                    "total_prompt_tokens": 100,
                    "total_completion_tokens": 50,
                    "total_latency_ms": 250, "by_provider": [],
                    "data_source": "ok",
                },
            )
            out = pipe._handle_cost(["/cost"], chat_id=_CHAT_A)
        assert "📌" in out
        called_url = mg.call_args[0][0]
        assert _SAMPLE_JOB_ID in called_url

    def test_no_arg_no_memory_returns_usage(self, pipe):
        out = pipe._handle_cost(["/cost"], chat_id=_CHAT_A)
        assert "Usage: `/cost <job_id>`" in out
        assert "📌" not in out

    def test_explicit_id_overrides_memory(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "total_cost_usd": 0.0, "call_count": 0,
                    "total_prompt_tokens": 0, "total_completion_tokens": 0,
                    "total_latency_ms": 0, "by_provider": [],
                    "data_source": "ok",
                },
            )
            out = pipe._handle_cost(
                ["/cost", _SAMPLE_JOB_ID_2], chat_id=_CHAT_A,
            )
        assert "📌" not in out
        called_url = mg.call_args[0][0]
        assert _SAMPLE_JOB_ID_2 in called_url


# ---------------------------------------------------------------------------
# Integration: end-to-end /idea → /results
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestEndToEndFlow:
    """Pin the canonical user flow: /idea sets, /results recalls."""

    def test_idea_then_results_no_args_uses_recalled_id(self, pipe):
        """The expected operator journey: type /idea, then /results
        with no args — the recalled id flows through automatically."""
        # /idea
        pipe._render_ideate_response(_ideate_response(), chat_id=_CHAT_A)
        # /results — no args
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "status": "running", "total_nodes": 5,
                    "counts": {"done": 2}, "nodes": [],
                    "next_actions": [],
                },
            )
            out = pipe._handle_results(["/results"], chat_id=_CHAT_A)
        # Recall hint surfaced.
        assert "📌" in out
        assert "abc1234e" in out
        # The journey is silent — no errors, no Usage strings.
        assert "Usage:" not in out
        # Request went to recalled id.
        called_url = mg.call_args[0][0]
        assert _SAMPLE_JOB_ID in called_url


# ---------------------------------------------------------------------------
# Dispatch integration: chat_id flows through _handle_command
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDispatchPlumbing:
    """§17.307 — `chat_id` must reach _handle_command from pipe() so
    the readers see it."""

    def test_handle_command_accepts_chat_id(self, pipe):
        """Signature contract: _handle_command takes chat_id kwarg.
        A change that drops it would silently disable the readers."""
        import inspect
        sig = inspect.signature(pipe._handle_command)
        assert "chat_id" in sig.parameters

    def test_handle_results_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._handle_results)
        assert "chat_id" in sig.parameters

    def test_handle_cost_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._handle_cost)
        assert "chat_id" in sig.parameters

    def test_render_ideate_response_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._render_ideate_response)
        assert "chat_id" in sig.parameters


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_remember_helper_anchored(self):
        src = self._src()
        assert "def _active_job_remember" in src

    def test_recall_helper_anchored(self):
        src = self._src()
        assert "def _active_job_recall" in src

    def test_hint_helper_anchored(self):
        src = self._src()
        assert "def _active_job_hint" in src

    def test_valve_anchored(self):
        """The pilot can be disabled — anchor the valve so a future
        "tidy up valves" pass doesn't drop the operator escape hatch."""
        src = self._src()
        assert "active_job_memory_enabled: bool = True" in src

    def test_pipe_passes_chat_id_to_handle_command(self):
        """The plumbing site in pipe() must thread chat_id through.
        Without this, _handle_command always sees chat_id=None and
        the readers never recall."""
        src = self._src()
        assert (
            "self._handle_command(\n                msg, chat_id=self._chat_id_from_body(body)"
            in src
        ), (
            "§17.307 regression: pipe() no longer threads chat_id "
            "into _handle_command. Readers will never recall."
        )

    def test_render_ideate_seeds_memory(self):
        """The writer site must invoke remember after extracting
        job_id. A refactor that drops the call would orphan the
        readers — they'd recall nothing because nothing seeded."""
        src = self._src()
        assert "self._active_job_remember(\n            chat_id, job_id" in src, (
            "§17.307 regression: /idea success no longer seeds the "
            "active-job cache. /results and /cost recall paths are "
            "dead."
        )
