"""§17.315 — `/exec retry` adopts §17.307 with tiered confirmation-friction.

§17.314 introduced the confirmation-friction model on /execute
(single-arg, all-or-nothing fire). /exec retry is the second
state-altering pilot — two-arg signature (job_id + node_key),
which lets the model become **tiered** based on operator
specificity:

  /exec retry                      — 0 args = 3 options (friction)
  /exec retry <UUID>               — 1 UUID arg = Usage error
  /exec retry <node_key>           — 1 non-UUID arg = auto-fire + 📌
  /exec retry <job_id> <node_key>  — 2 args = explicit (unchanged)

The middle two are the §17.315 contribution:

  - UUID single-arg means operator typed job_id but forgot the
    node_key. Auto-guessing the node_key from recall would be
    state-altering on a guess — refuse. Point at the 2-arg form.
  - Non-UUID single-arg means operator specified the node_key
    deliberately. Auto-substituting job_id from recall is safe
    because the failure mode (wrong job for the node_key) is a
    visible 404, not destructive.

Why no friction on the auto-substitute path: operator already
showed deliberateness by typing the node_key. The "muscle-memory
fires on wrong job" risk (which §17.314 guards against for bare
/execute) doesn't apply — typing `T3` is not muscle-memory.

These tests pin each of the 5 paths + source-shape guards.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"
_OTHER_JOB_ID = "ffff1111-2222-3333-4444-555566667777"
_CHAT_A = "chat-aaa-111"


def _dispatch(pipe, msg: str, chat_id: str | None = None) -> str:
    """Drive the /exec dispatch the same way _handle_command does."""
    parts = msg.split(None, 2)
    return pipe._handle_exec(parts, chat_id=chat_id)


def _ok_retry_response() -> MagicMock:
    """Mock the orchestrator's /exec/retry response."""
    r = MagicMock(status_code=200, text="")
    r.json.return_value = {"job_id": _SAMPLE_JOB_ID, "node_key": "T3",
                           "status": "pending"}
    return r


# ---------------------------------------------------------------------------
# Tier 1: zero args = 3 options friction (mirrors §17.314)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestBareRetryWithRecall:
    """§17.315 — `/exec retry` (no args) on recall hit shows 📌 + 3
    options. Mirror of §17.314's /execute confirmation surface."""

    def test_recall_hit_shows_active_job(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my CLI")
        out = _dispatch(pipe, "/exec retry", chat_id=_CHAT_A)
        assert "📌" in out
        assert "abc1234e" in out
        assert "my CLI" in out

    def test_recall_hit_does_not_fire_post(self, pipe):
        """The critical contract — bare /exec retry must NOT issue
        the orchestrator POST. Mirror of §17.314."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            _dispatch(pipe, "/exec retry", chat_id=_CHAT_A)
        mp.assert_not_called()

    def test_recall_hit_warns_state_altering(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        out = _dispatch(pipe, "/exec retry", chat_id=_CHAT_A)
        assert "state-altering" in out.lower()
        assert "re-runs a failed/blocked node" in out

    def test_recall_hit_offers_all_three_options(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        out = _dispatch(pipe, "/exec retry", chat_id=_CHAT_A)
        # Option 1: short form on active job.
        assert "/exec retry <node_key>" in out
        # Option 2: full explicit.
        assert "/exec retry <other_job_id> <node_key>" in out
        # Option 3: escape hatch (diagnose first).
        assert "/results abc1234e" in out

    def test_no_recall_falls_back_to_usage(self, pipe):
        """Cold cache + bare /exec retry = pre-§17.315 Usage error."""
        out = _dispatch(pipe, "/exec retry", chat_id=_CHAT_A)
        assert "Usage: `/exec retry <job_id> <node_key>`" in out
        assert "📌" not in out


# ---------------------------------------------------------------------------
# Tier 2: single UUID arg = ambiguous, Usage error (refuse to guess)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSingleUUIDArgRefused:
    """§17.315 — `/exec retry <UUID>` means operator typed job_id but
    forgot node_key. Guessing the node_key from any source would be
    state-altering on a guess. Refuse + point at the 2-arg form."""

    def test_single_uuid_arg_returns_usage(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = _dispatch(
                pipe, f"/exec retry {_SAMPLE_JOB_ID}", chat_id=_CHAT_A,
            )
        # No fire — refused.
        mp.assert_not_called()
        # Usage error pointing at 2-arg form.
        assert "Usage: `/exec retry <job_id> <node_key>`" in out
        # Pre-filled with the short id operator typed (saves re-paste).
        assert _SAMPLE_JOB_ID[:8] in out

    def test_single_uuid_no_chat_id_returns_usage(self, pipe):
        """Same Usage shape with no chat_id — recall isn't even
        considered because the arg looks like job_id, not node_key."""
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = _dispatch(
                pipe, f"/exec retry {_OTHER_JOB_ID}", chat_id=None,
            )
        mp.assert_not_called()
        assert "Usage:" in out


# ---------------------------------------------------------------------------
# Tier 3: single non-UUID arg = auto-substitute job_id from recall
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSingleNodeKeyAutoFire:
    """§17.315 — `/exec retry <node_key>` with recall hit auto-fires
    on the recalled job_id + node_key. Operator specified the node
    deliberately; no friction needed."""

    def test_single_node_recall_hit_auto_fires(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my CLI")
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _ok_retry_response()
            out = _dispatch(pipe, "/exec retry T3", chat_id=_CHAT_A)
        # POST issued with recalled job_id + typed node_key.
        mp.assert_called_once()
        sent_json = mp.call_args[1]["json"]
        assert sent_json["job_id"] == _SAMPLE_JOB_ID
        assert sent_json["node_key"] == "T3"
        # 📌 hint surfaced (operator sees what fired).
        assert "📌" in out
        assert "abc1234e" in out

    def test_single_node_no_recall_returns_error(self, pipe):
        """Cold cache + single node_key = friendly error pointing at
        2-arg form. NO auto-fire."""
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = _dispatch(pipe, "/exec retry T3", chat_id=_CHAT_A)
        mp.assert_not_called()
        assert "No active job" in out
        # Error pre-fills the node_key the operator typed.
        assert "/exec retry <job_id> T3" in out

    def test_single_node_no_chat_id_returns_error(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = _dispatch(pipe, "/exec retry T3", chat_id=None)
        mp.assert_not_called()
        assert "No active job" in out

    def test_single_node_placeholder_rejected(self, pipe):
        """Single arg that's a placeholder (e.g., `<node_key>`)
        should be rejected — pre-§17.315 contract."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = _dispatch(pipe, "/exec retry <node_key>", chat_id=_CHAT_A)
        mp.assert_not_called()
        assert "placeholder" in out.lower()


# ---------------------------------------------------------------------------
# Tier 4: 2-arg explicit (existing path, unchanged)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExplicitTwoArgUnchanged:

    def test_two_arg_explicit_fires(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _ok_retry_response()
            out = _dispatch(
                pipe, f"/exec retry {_OTHER_JOB_ID} T7", chat_id=_CHAT_A,
            )
        mp.assert_called_once()
        sent_json = mp.call_args[1]["json"]
        assert sent_json["job_id"] == _OTHER_JOB_ID
        assert sent_json["node_key"] == "T7"
        # No 📌 — explicit path doesn't consult recall.
        assert "📌" not in out

    def test_two_arg_explicit_overrides_recall(self, pipe):
        """Explicit 2-arg must NEVER consult the cache — mirror of
        §17.314's contract."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="cached")
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _ok_retry_response()
            _dispatch(
                pipe, f"/exec retry {_OTHER_JOB_ID} T7", chat_id=_CHAT_A,
            )
        sent_json = mp.call_args[1]["json"]
        # Uses EXPLICIT id, not cached.
        assert sent_json["job_id"] == _OTHER_JOB_ID

    def test_two_arg_placeholder_rejected(self, pipe):
        """§17.301 placeholder check on both args still fires."""
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            out = _dispatch(pipe, "/exec retry <job_id> T2", chat_id=_CHAT_A)
        mp.assert_not_called()
        assert "placeholder" in out.lower()


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_handle_exec_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._handle_exec)
        assert "chat_id" in sig.parameters

    def test_dispatch_passes_chat_id(self):
        src = self._src()
        assert "self._handle_exec(parts, chat_id=chat_id)" in src

    def test_three_tier_branches_anchored(self):
        """Pin the tiered model's three-branch shape — zero args,
        single arg, two args. A refactor that collapses them
        regresses the model."""
        src = self._src()
        assert "if len(tail) == 0:" in src
        assert "if len(tail) == 1:" in src

    def test_uuid_single_arg_refused(self):
        """The ambiguous-UUID branch must point at the 2-arg form."""
        src = self._src()
        assert "Looks like you typed a job_id" in src

    def test_auto_substitute_path_anchored(self):
        """The non-UUID single-arg branch fires POST with recalled
        job_id + typed node_key. Pin the recall + post combination."""
        src = self._src()
        assert "self._active_job_recall(chat_id)" in src
        assert '"node_key": only' in src
