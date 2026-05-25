"""§17.311 — `/logs` adopts §17.307's active-job memory.

§17.307 piloted in-pipeline chat_id → job_id memory on /results +
/cost — the two highest-frequency read-only id-takers. §17.311
extends the pattern to /logs (third read-only id-taker) after
validating the pilot's UX held up across §17.307-§17.310.

Same contract as §17.307:
  - WRITER: unchanged. /idea success seeds the cache (§17.307).
  - READER: /logs invoked with NO explicit id recalls + prepends
    the 📌 hint; cache miss = §17.301-style Usage error (richer
    than the pre-§17.311 terse one-liner).
  - Explicit args always override.
  - No chat_id = no recall (curl-only callers unchanged).

Also upgrades the pre-§17.311 Usage one-liner ("Usage: `/logs
<job_id>`") to match the §17.301 richer shape (Example + 💡 hint
pointing at /jobs).

These tests pin: recall hit / cache miss / explicit override /
no chat_id; the richer Usage error shape; dispatch plumbing;
source-shape regression guards.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"
_SAMPLE_JOB_ID_2 = "ffff1111-2222-3333-4444-555566667777"
_CHAT_A = "chat-aaa-111"


def _logs_response(nodes: list | None = None) -> MagicMock:
    """Mock the orchestrator /logs/<id> response."""
    r = MagicMock(status_code=200, text="")
    r.json.return_value = {
        "nodes": nodes or [],
        "node_count": len(nodes or []),
        "job_status": "running",
    }
    return r


# ---------------------------------------------------------------------------
# Recall hit — /logs (no args) with cache populated
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestLogsReader:

    def test_no_arg_with_memory_uses_cached_id(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my job")
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _logs_response(
                [{"node_key": "T1", "status": "done", "tool": "LLM"}],
            )
            out = pipe._handle_logs(["/logs"], chat_id=_CHAT_A)
        # 📌 hint prepended.
        assert "📌" in out
        # Standard body follows.
        assert "🪵 Logs" in out
        # Request went to recalled id.
        called_url = mg.call_args[0][0]
        assert _SAMPLE_JOB_ID in called_url

    def test_no_arg_no_memory_returns_richer_usage(self, pipe):
        """§17.311 also upgrades the Usage error — pre-§17.311 the
        message was a bare `Usage: /logs <job_id>`. Post-§17.311 it
        matches §17.301's richer shape (Example + 💡 /jobs hint)."""
        out = pipe._handle_logs(["/logs"], chat_id=_CHAT_A)
        assert "Usage:" in out
        assert "`/logs <job_id>`" in out
        # §17.311 — richer Usage matching §17.301's /results /
        # /dag / /jobs patterns.
        assert "Example:" in out
        assert "/jobs" in out
        # No 📌 because nothing was recalled.
        assert "📌" not in out

    def test_explicit_id_overrides_memory(self, pipe):
        """Explicit id takes priority — no hint, no substitution."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _logs_response()
            out = pipe._handle_logs(
                ["/logs", _SAMPLE_JOB_ID_2], chat_id=_CHAT_A,
            )
        assert "📌" not in out
        called_url = mg.call_args[0][0]
        assert _SAMPLE_JOB_ID_2 in called_url
        assert _SAMPLE_JOB_ID not in called_url

    def test_no_chat_id_no_recall(self, pipe):
        """Without chat_id, recall can't fire even with cache populated.
        Falls through to richer Usage."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = pipe._handle_logs(["/logs"], chat_id=None)
        assert "Usage:" in out
        assert "📌" not in out

    def test_placeholder_still_rejected(self, pipe):
        """§17.301 placeholder check unchanged — explicit `<id>` shape
        is rejected even on the recall path (defensive, since the
        recall stores actual ids not placeholders)."""
        out = pipe._handle_logs(["/logs", "<job_id>"], chat_id=_CHAT_A)
        assert "missing or a placeholder" in out


# ---------------------------------------------------------------------------
# Recursion-as-prepend pattern unchanged
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestRecursionPattern:
    """§17.311 reuses §17.307's recursion pattern: recall, then
    self-call with explicit id, then prepend the hint to the result."""

    def test_404_path_still_works_with_recall(self, pipe):
        """If the recalled id 404s (stale cache), the error message
        from /logs's existing 4xx path is preserved + the hint."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="stale")
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            r = MagicMock(status_code=404, text="not found")
            r.json.return_value = {"detail": "not found"}
            mg.return_value = r
            out = pipe._handle_logs(["/logs"], chat_id=_CHAT_A)
        # Hint surfaced (operator sees they used a stale recall).
        assert "📌" in out
        # Existing /logs 4xx fallback (via _fmt).
        assert "Error 404" in out or "not found" in out

    def test_empty_nodes_branch_with_recall(self, pipe):
        """Job exists but no DAG nodes yet — pre-§17.311 _handle_logs
        returns a parenthetical line. Recall path must still prepend."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="empty")
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = _logs_response(nodes=[])
            out = pipe._handle_logs(["/logs"], chat_id=_CHAT_A)
        assert "📌" in out
        assert "no DAG nodes" in out


# ---------------------------------------------------------------------------
# Cross-chat isolation
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestCrossChatIsolation:

    def test_chat_b_does_not_see_chat_a_memory(self, pipe):
        """Memory is per-chat — operator in chat B doesn't recall chat
        A's job. Mirror of §17.307's contract for /results + /cost."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = pipe._handle_logs(["/logs"], chat_id="chat-bbb-222")
        assert "Usage:" in out
        assert "📌" not in out


# ---------------------------------------------------------------------------
# Dispatch plumbing
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDispatchPlumbing:

    def test_handle_logs_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._handle_logs)
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

    def test_logs_dispatch_passes_chat_id(self):
        src = self._src()
        assert "self._handle_logs(parts, chat_id=chat_id)" in src

    def test_logs_handler_uses_recall_recursion_pattern(self):
        """The recursion pattern (recall → recursive call with explicit
        id → hint prepended) is the §17.307 contract. Anchor it."""
        src = self._src()
        # Pin the specific pattern in _handle_logs's body.
        assert "hint + self._handle_logs([parts[0], rid])" in src, (
            "§17.311 regression: /logs no longer uses the recursion-as-"
            "prepend pattern. The recall path would produce a stale "
            "hint glued to wrong body."
        )

    def test_logs_usage_error_matches_17_301_shape(self):
        """The richer Usage error matches §17.301's pattern across
        /results, /dag, /execute, /jobs rename/delete. Pin the shape."""
        src = self._src()
        # The Example line is the §17.301 signature.
        assert "Example: `/logs 01ab243e`" in src
        # The 💡 /jobs lookup hint.
        assert "💡 Use `/jobs` to list your active jobs" in src
