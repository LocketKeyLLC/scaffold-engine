"""§17.314 — `/execute` adopts §17.307's pattern with confirmation-friction.

§17.307 piloted in-pipeline chat_id → job_id memory on read-only
commands (/results, /cost; later /logs in §17.311). State-altering
commands (/execute, /exec retry, /skip) were deliberately deferred
— a muscle-memory bare invocation on a recalled id is the failure
mode the pilot was designed to avoid.

§17.314 introduces the **confirmation-friction model**: state-
altering recall is OPT-IN via an explicit `confirm` word. `/execute`
is the pilot — simplest of the cohort (single-arg, terminal action).

Behavior:

  /execute                  — show 📌 + 3 options, no action
  /execute confirm          — use recalled id, fire, show 📌 hint
  /execute confirm  (cold)  — friendly error pointing at /execute <id>
  /execute <job_id>         — explicit, fires (no recall consulted)
  /execute <placeholder>    — §17.301 rejection (unchanged)

The 3 options on the recall-hit screen:
  1. /execute confirm     — proceed on recalled
  2. /execute <other_id>  — target a different job
  3. /results <short_id>  — diagnose first (escape hatch)

The escape-hatch is load-bearing: operators in the recall path may
have FORGOTTEN they have an active job and need to look before
acting. The 3rd option teaches the diagnose-first pattern.

These tests pin each path + the 3-options surface + the escape-
hatch + source-shape guards.
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


def _drive_execute(pipe, msg: str, chat_id: str | None = None) -> str:
    """Run pipe._handle_execute with execute streaming stubbed.
    Returns the joined output for inspection."""
    def _stub_stream(job_id, offset):
        yield f"STUBBED_EXECUTE_STREAM job_id={job_id} offset={offset}"

    with patch.object(pipe, "_execute_and_stream", side_effect=_stub_stream):
        chunks = list(pipe._handle_execute(msg, chat_id=chat_id))
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Bare `/execute` with active-job recall → 3-options surface
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestRecallHitOptions:
    """§17.314 — bare /execute with an active job in chat memory must
    NOT fire. It shows 📌 + 3 deliberate-action options."""

    def test_recall_hit_shows_active_job(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my CLI")
        out = _drive_execute(pipe, "/execute", chat_id=_CHAT_A)
        # 📌 marker + short id surfaced.
        assert "📌" in out
        assert "abc1234e" in out
        # Title from recall.
        assert "my CLI" in out

    def test_recall_hit_does_not_execute(self, pipe):
        """The critical contract — bare /execute on recall hit must
        NOT fire the orchestrator stream. The 3-options surface is
        the entire output."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        out = _drive_execute(pipe, "/execute", chat_id=_CHAT_A)
        assert "STUBBED_EXECUTE_STREAM" not in out

    def test_recall_hit_warns_state_altering(self, pipe):
        """The 📌 hint MUST surface that /execute is state-altering.
        Confirmation-friction model depends on operator seeing the
        warning before they type `confirm`."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        out = _drive_execute(pipe, "/execute", chat_id=_CHAT_A)
        assert "state-altering" in out.lower()
        # The action description names "pending DAG nodes" so operator
        # knows what /execute does.
        assert "pending DAG nodes" in out

    def test_recall_hit_offers_all_three_options(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        out = _drive_execute(pipe, "/execute", chat_id=_CHAT_A)
        # Option 1: confirm to proceed.
        assert "/execute confirm" in out
        # Option 2: explicit other id.
        assert "/execute <other_job_id>" in out
        # Option 3: escape hatch (diagnose first).
        assert "/results abc1234e" in out


# ---------------------------------------------------------------------------
# `/execute confirm` — fires on recalled id
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExecuteConfirm:

    def test_confirm_with_recall_executes_on_recalled_id(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="my CLI")
        out = _drive_execute(pipe, "/execute confirm", chat_id=_CHAT_A)
        # 📌 hint surfaced (operator sees what fired).
        assert "📌" in out
        # Streaming fired on recalled id.
        assert "STUBBED_EXECUTE_STREAM" in out
        assert f"job_id={_SAMPLE_JOB_ID}" in out
        # The "Executing all nodes" line uses the recalled id.
        assert f"Executing all nodes for job `{_SAMPLE_JOB_ID}`" in out

    def test_confirm_without_recall_returns_friendly_error(self, pipe):
        """`/execute confirm` with NO cached job — explicit error
        pointing at /execute <id>, NOT a silent no-op."""
        out = _drive_execute(pipe, "/execute confirm", chat_id=_CHAT_A)
        assert "requires an active job" in out
        assert "/execute <job_id>" in out
        # No streaming fired.
        assert "STUBBED_EXECUTE_STREAM" not in out

    def test_confirm_without_chat_id_returns_friendly_error(self, pipe):
        """Curl-only callers — chat_id is None → can't recall →
        friendly error. No streaming."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = _drive_execute(pipe, "/execute confirm", chat_id=None)
        assert "requires an active job" in out
        assert "STUBBED_EXECUTE_STREAM" not in out

    def test_confirm_case_insensitive(self, pipe):
        """Operators type CONFIRM or Confirm; honor both."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        out = _drive_execute(pipe, "/execute CONFIRM", chat_id=_CHAT_A)
        assert "STUBBED_EXECUTE_STREAM" in out


# ---------------------------------------------------------------------------
# Explicit id always overrides recall
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExplicitIdOverridesRecall:
    """§17.314 — explicit id never consults the cache. State-altering
    contract: operator typed the id they meant; honor it directly."""

    def test_explicit_id_executes_on_explicit_id(self, pipe):
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID, title="x")
        out = _drive_execute(
            pipe, f"/execute {_OTHER_JOB_ID}", chat_id=_CHAT_A,
        )
        # No 📌 — explicit path doesn't surface the recall.
        assert "📌" not in out
        # Streaming on the EXPLICIT id, not the recalled one.
        assert f"job_id={_OTHER_JOB_ID}" in out
        assert f"job_id={_SAMPLE_JOB_ID}" not in out

    def test_explicit_id_with_no_recall_still_works(self, pipe):
        out = _drive_execute(
            pipe, f"/execute {_OTHER_JOB_ID}", chat_id=_CHAT_A,
        )
        assert f"job_id={_OTHER_JOB_ID}" in out
        assert "📌" not in out


# ---------------------------------------------------------------------------
# Cold-cache bare /execute — pre-§17.314 Usage error preserved
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestColdCacheBare:

    def test_bare_no_recall_falls_back_to_usage(self, pipe):
        """Same Usage error as pre-§17.314 when there's no cached
        job (mirror of §17.307's no-surprise contract)."""
        out = _drive_execute(pipe, "/execute", chat_id=_CHAT_A)
        assert "Usage: `/execute <job_id>`" in out
        assert "Example:" in out
        assert "/jobs" in out
        # No 📌, no STUBBED stream.
        assert "📌" not in out
        assert "STUBBED_EXECUTE_STREAM" not in out

    def test_bare_no_chat_id_falls_back_to_usage(self, pipe):
        """Curl-only callers — no chat_id → no recall → Usage."""
        pipe._active_job_remember(_CHAT_A, _SAMPLE_JOB_ID)
        out = _drive_execute(pipe, "/execute", chat_id=None)
        assert "Usage: `/execute <job_id>`" in out


# ---------------------------------------------------------------------------
# Placeholder rejection preserved (§17.301 contract)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestPlaceholderRejection:

    def test_placeholder_rejected(self, pipe):
        """`/execute <job_id>` literal placeholder still rejected."""
        out = _drive_execute(pipe, "/execute <job_id>", chat_id=_CHAT_A)
        assert "placeholder" in out.lower()
        assert "STUBBED_EXECUTE_STREAM" not in out


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_handle_execute_accepts_chat_id(self, pipe):
        import inspect
        sig = inspect.signature(pipe._handle_execute)
        assert "chat_id" in sig.parameters

    def test_pipe_dispatch_passes_chat_id(self):
        src = self._src()
        assert "self._handle_execute(\n                msg, chat_id=self._chat_id_from_body(body)" in src, (
            "§17.314 regression: pipe()'s /execute dispatch no "
            "longer threads chat_id into _handle_execute. The "
            "confirmation-friction recall path is dead."
        )

    def test_confirm_branch_anchored(self):
        """The confirm-keyword branch is the load-bearing piece of
        the friction model. Pin its detection."""
        src = self._src()
        assert 'parts[1].lower() == "confirm"' in src

    def test_state_altering_warning_anchored(self):
        """The "state-altering" warning is the operator-visible cue
        the friction model depends on. Pin its phrasing."""
        src = self._src()
        assert "state-altering" in src
        # The action description that names what /execute does.
        assert "runs ALL pending DAG nodes" in src

    def test_three_options_anchored(self):
        """Pin each of the 3 option-template strings so a refactor
        that drops one (e.g., removes the diagnose-first escape) is
        visible at review."""
        src = self._src()
        assert "/execute confirm" in src
        assert "/execute <other_job_id>" in src
        # The escape hatch — /results pre-filled with the short id.
        assert "check the job first: `/results" in src
