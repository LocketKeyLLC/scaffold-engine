"""§17.322 — `/cancel <job_id>` pipeline command tests.

Mirrors test_scaffold_router_execute_confirm.py's structure because
/cancel adopts the same §17.314 confirmation-friction recall pattern
(state-altering, so bare /cancel never auto-fires).

Coverage:
  - Bare /cancel + recall hit → 📌 + 3-options surface, no POST
  - Bare /cancel + cold cache → Usage error
  - /cancel confirm + recall hit → POST on recalled id with 📌 hint
  - /cancel confirm + cold cache → friendly error pointing at explicit form
  - /cancel <job_id> → POST (explicit; no friction)
  - /cancel <not-a-uuid> → invalid-id error
  - /cancel <placeholder> → §17.301 rejection
  - Response rendering for the 3 CancelJobResult shapes (200-new, 200-
    idempotent, error passthrough)
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response


@pytest.fixture
def pipe():
    return Pipeline()


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"
_OTHER_JOB_ID = "ffff1111-2222-3333-4444-555566667777"
_CHAT_A = "chat-aaa-111"


def _drive_cancel(pipe, msg: str, chat_id: str | None = None,
                  response=None) -> str:
    """Invoke pipe._handle_cancel with the HTTP POST stubbed.
    Returns the rendered string."""
    parts = msg.split()
    if response is None:
        response = _make_response(200, {
            "id": _SAMPLE_JOB_ID,
            "cancelled": True,
            "was_already_cancelled": False,
            "status_before": "awaiting_confirmation",
            "status_after": "cancelled",
        })
    with patch.object(
        pipe.__class__, "_post_cancel", autospec=True
    ) as m:
        m.side_effect = lambda self, jid: f"POSTED job={jid}"
        out = pipe._handle_cancel(parts, chat_id=chat_id)
    return out


# ---------------------------------------------------------------------------
# Bare /cancel — confirmation-friction recall surface
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestBareCancelRecallHit:
    """Bare /cancel with an active job MUST NOT fire. Show 📌 + 3 options."""

    def test_recall_hit_shows_active_job(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="sort algos")
        out = pipe._handle_cancel(["/cancel"], chat_id=_CHAT_A)
        assert "📌" in out
        assert "abc1234e" in out
        assert "sort algos" in out

    def test_recall_hit_does_not_post(self, pipe):
        """The 0-args path must not call _post_cancel — that's the
        whole point of confirmation-friction."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch.object(pipe.__class__, "_post_cancel") as m:
            pipe._handle_cancel(["/cancel"], chat_id=_CHAT_A)
        m.assert_not_called()

    def test_recall_hit_shows_three_options(self, pipe):
        """Three deliberate-action options + escape-hatch (/status)."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = pipe._handle_cancel(["/cancel"], chat_id=_CHAT_A)
        assert "/cancel confirm" in out
        assert "/cancel <other_job_id>" in out
        assert "/status" in out

    def test_recall_hit_warns_state_altering_but_reversible(self, pipe):
        """The warning text must communicate state-altering AND that
        /resume is the reversal path — operators should not fear /cancel."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = pipe._handle_cancel(["/cancel"], chat_id=_CHAT_A)
        assert "state-altering" in out
        assert "/resume" in out


@pytest.mark.smoke
class TestBareCancelCold:
    """Bare /cancel with no active job → Usage error pointing at /jobs."""

    def test_cold_returns_usage(self, pipe):
        out = pipe._handle_cancel(["/cancel"], chat_id="chat-empty")
        assert "Usage" in out
        assert "/cancel <job_id>" in out

    def test_cold_points_at_jobs_list(self, pipe):
        out = pipe._handle_cancel(["/cancel"], chat_id=None)
        assert "/jobs" in out


# ---------------------------------------------------------------------------
# /cancel confirm — recall-required fire
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestCancelConfirmHit:
    """/cancel confirm fires on the recalled id with 📌 hint prepended."""

    def test_confirm_hit_calls_post(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="sort algos")
        with patch.object(
            pipe.__class__, "_post_cancel", autospec=True
        ) as m:
            m.return_value = "POSTED"
            out = pipe._handle_cancel(
                ["/cancel", "confirm"], chat_id=_CHAT_A,
            )
        m.assert_called_once_with(pipe, _SAMPLE_JOB_ID)
        # 📌 hint prepended.
        assert "📌" in out
        assert "POSTED" in out

    def test_confirm_cold_returns_error(self, pipe):
        """No active job → error pointing at explicit form (does NOT fire)."""
        with patch.object(pipe.__class__, "_post_cancel") as m:
            out = pipe._handle_cancel(
                ["/cancel", "confirm"], chat_id="chat-empty",
            )
        m.assert_not_called()
        assert "❌" in out
        assert "/cancel <job_id>" in out


# ---------------------------------------------------------------------------
# /cancel <job_id> — explicit, no friction
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestExplicitCancel:
    """An explicit job_id arg fires without recall consultation."""

    def test_explicit_uuid_calls_post(self, pipe):
        with patch.object(
            pipe.__class__, "_post_cancel", autospec=True
        ) as m:
            m.return_value = "POSTED"
            out = pipe._handle_cancel(
                ["/cancel", _SAMPLE_JOB_ID], chat_id=_CHAT_A,
            )
        m.assert_called_once_with(pipe, _SAMPLE_JOB_ID)
        assert out == "POSTED"  # no 📌 hint on explicit path

    def test_explicit_short_id_calls_post(self, pipe):
        """8-hex-char short_id is the canonical operator-typed form."""
        short = "01ab243e"
        with patch.object(
            pipe.__class__, "_post_cancel", autospec=True
        ) as m:
            m.return_value = "POSTED"
            pipe._handle_cancel(
                ["/cancel", short], chat_id=None,
            )
        m.assert_called_once_with(pipe, short)

    def test_explicit_ignores_recall(self, pipe):
        """Explicit id must NOT consult recall — operator deliberately
        typed; recall is for muscle-memory paths."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="A")
        with patch.object(
            pipe.__class__, "_post_cancel", autospec=True
        ) as m:
            m.return_value = "X"
            pipe._handle_cancel(
                ["/cancel", _OTHER_JOB_ID], chat_id=_CHAT_A,
            )
        # POST sent for the EXPLICIT id, not the recalled one.
        m.assert_called_once_with(pipe, _OTHER_JOB_ID)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestInputValidation:
    """Bad arguments must surface a friendly error without POSTing."""

    def test_placeholder_rejected(self, pipe):
        """§17.301 placeholder check — `<job_id>` etc. must be rejected."""
        with patch.object(pipe.__class__, "_post_cancel") as m:
            out = pipe._handle_cancel(
                ["/cancel", "<job_id>"], chat_id=None,
            )
        m.assert_not_called()
        assert "placeholder" in out.lower()

    def test_non_uuid_rejected(self, pipe):
        """Random string that's neither UUID nor short_id → friendly error."""
        with patch.object(pipe.__class__, "_post_cancel") as m:
            out = pipe._handle_cancel(
                ["/cancel", "not-a-real-id"], chat_id=None,
            )
        m.assert_not_called()
        assert "❌" in out
        assert "job_id" in out


# ---------------------------------------------------------------------------
# _post_cancel rendering
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestPostCancelRendering:
    """The three CancelJobResult body shapes (success / idempotent /
    error) must each render distinct operator-facing text."""

    def test_renders_success_with_prior_status(self, pipe):
        resp = _make_response(200, {
            "id": _SAMPLE_JOB_ID,
            "cancelled": True,
            "was_already_cancelled": False,
            "status_before": "awaiting_confirmation",
            "status_after": "cancelled",
        })
        with patch(
            "scaffold_router._HTTP_SESSION.post", return_value=resp,
        ):
            out = pipe._post_cancel(_SAMPLE_JOB_ID)
        assert "🛑" in out
        assert "abc1234e" in out
        assert "awaiting_confirmation" in out
        # Reversal hint anchored — operators must always know /resume exists.
        assert "/resume" in out

    def test_renders_idempotent_already_cancelled(self, pipe):
        resp = _make_response(200, {
            "id": _SAMPLE_JOB_ID,
            "cancelled": True,
            "was_already_cancelled": True,
            "status_before": "cancelled",
            "status_after": "cancelled",
        })
        with patch(
            "scaffold_router._HTTP_SESSION.post", return_value=resp,
        ):
            out = pipe._post_cancel(_SAMPLE_JOB_ID)
        assert "ℹ️" in out
        assert "already cancelled" in out
        # Even idempotent OK must surface the /resume option — operator
        # may have arrived expecting the cancel and is now stuck.
        assert "/resume" in out

    def test_renders_error_passthrough_on_non_200(self, pipe):
        """Non-200 (e.g. 409 terminal status) renders via _fmt — no
        custom shaping in /cancel, just clean error pass-through."""
        resp = _make_response(409, {
            "detail": {
                "error": "job not cancellable",
                "current_status": "completed",
            }
        })
        with patch(
            "scaffold_router._HTTP_SESSION.post", return_value=resp,
        ):
            out = pipe._post_cancel(_SAMPLE_JOB_ID)
        # _fmt formats the response; we just check the 409 detail flows through.
        assert "completed" in out
