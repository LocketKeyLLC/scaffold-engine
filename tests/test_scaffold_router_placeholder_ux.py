"""§17.301 — placeholder + missing-arg UX for job_id-taking commands.

Five commands updated: `/dag`, `/results`, `/execute`, `/jobs rename`,
`/jobs delete`. Each now follows the §17.300 shape established by
`/idea`'s pre-existing message + the welcome preamble:

  1. Missing arg → Usage line + Example line + 💡 hint with the
     copy-pasteable `/jobs` lookup command.
  2. Placeholder-looking arg (`<job_id>`, `job_id`, etc.) → friendly
     "looks like job_id is missing or a placeholder" reply that
     suggests the canonical example.

The 8-char `01ab243e` fragment is the codebase-wide convention for
job_id examples (mirrors `/skip`, `/exec retry`, `/logs`, `/cost`
pre-§17.301 messages).
"""
from unittest.mock import patch, MagicMock

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _drive_generator(gen) -> str:
    """Join chunks from a yield-based handler."""
    return "".join(gen)


# ---------------------------------------------------------------------------
# /dag — sync command via _handle_command path
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDagPlaceholderUx:

    def test_no_arg_shows_usage_with_example(self, pipe):
        out = pipe._handle_command("/dag")
        assert "Usage:" in out and "/dag <job_id>" in out
        assert "01ab243e" in out
        assert "/jobs" in out

    def test_placeholder_arg_rejected(self, pipe):
        out = pipe._handle_command("/dag <job_id>")
        assert "missing or a placeholder" in out
        assert "01ab243e" in out

    def test_bare_placeholder_word_rejected(self, pipe):
        """`/dag job_id` (no brackets) is the PLACEHOLDER_TOKENS case."""
        out = pipe._handle_command("/dag job_id")
        assert "missing or a placeholder" in out

    def test_real_id_passes_through(self, pipe):
        """A real 8-char id isn't rejected — falls through to the HTTP
        call. Mock the HTTP call so we don't actually POST."""
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = MagicMock(status_code=200, text="ok")
            mp.return_value.json.return_value = {"status": "ok"}
            out = pipe._handle_command("/dag abcd1234")
        # No placeholder banner — the call went through.
        assert "missing or a placeholder" not in out


# ---------------------------------------------------------------------------
# /results
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestResultsPlaceholderUx:

    def test_no_arg_shows_usage_with_example(self, pipe):
        out = pipe._handle_results(["/results"])
        assert "Usage:" in out and "/results <job_id>" in out
        assert "01ab243e" in out
        assert "/jobs" in out

    def test_placeholder_arg_rejected(self, pipe):
        out = pipe._handle_results(["/results", "<job_id>"])
        assert "missing or a placeholder" in out
        assert "01ab243e" in out

    def test_real_id_passes_through(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(status_code=200)
            mg.return_value.json.return_value = {
                "status": "completed",
                "compiled_output": "DONE",
            }
            out = pipe._handle_results(["/results", "abcd1234"])
        assert "DONE" in out


# ---------------------------------------------------------------------------
# /execute — generator-yielded
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestExecutePlaceholderUx:

    def test_no_arg_shows_usage_with_example(self, pipe):
        out = _drive_generator(pipe._handle_execute("/execute"))
        assert "Usage:" in out and "/execute <job_id>" in out
        assert "01ab243e" in out
        assert "/jobs" in out

    def test_placeholder_arg_rejected(self, pipe):
        out = _drive_generator(pipe._handle_execute("/execute <job_id>"))
        assert "missing or a placeholder" in out
        assert "01ab243e" in out

    def test_real_id_passes_through(self, pipe):
        """Real id should NOT trip the placeholder check — it reaches the
        SSE-streaming path."""
        # Mock the streaming so we don't actually start a real SSE.
        with patch.object(pipe, "_execute_and_stream", return_value=iter([])):
            out = _drive_generator(pipe._handle_execute("/execute abcd1234"))
        # The "Executing all nodes" banner means the placeholder check passed.
        assert "Executing all nodes" in out
        assert "missing or a placeholder" not in out


# ---------------------------------------------------------------------------
# /jobs rename + delete
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestJobsRenamePlaceholderUx:

    def test_no_args_shows_usage(self, pipe):
        out = pipe._handle_command("/jobs rename")
        assert "Usage:" in out and "/jobs rename" in out
        assert "01ab243e" in out
        assert "/jobs" in out

    def test_placeholder_job_id_rejected(self, pipe):
        out = pipe._handle_command("/jobs rename <job_id> NewTitle")
        assert "job_id is missing" in out
        assert "01ab243e" in out

    def test_placeholder_title_rejected(self, pipe):
        """Title field also gets the placeholder check — distinct from
        job_id so the error names the right arg."""
        out = pipe._handle_command("/jobs rename abcd1234 <new title>")
        assert "new title is missing" in out


@pytest.mark.smoke
class TestJobsDeletePlaceholderUx:

    def test_no_arg_shows_usage_and_confirm_hint(self, pipe):
        """The delete usage message must mention the `confirm` shortcut
        so an operator who skips the prompt knows the syntax."""
        out = pipe._handle_command("/jobs delete")
        assert "Usage:" in out and "/jobs delete" in out
        assert "01ab243e" in out
        assert "confirm" in out

    def test_placeholder_arg_rejected(self, pipe):
        out = pipe._handle_command("/jobs delete <job_id>")
        assert "missing or a placeholder" in out
        assert "01ab243e" in out


# ---------------------------------------------------------------------------
# Source-shape regression guards
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.301 — anchor the consistency convention so future commands
    follow the same shape."""

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_example_job_id_is_consistent(self):
        """`01ab243e` is the codebase-wide example fragment. New
        usage messages should use the same fragment so operators
        learn one canonical example, not five."""
        src = self._src()
        # The five §17.301 commands should all contain `01ab243e` in
        # their usage / placeholder messages. Count is ≥ 5 (each fix
        # site uses it at least once; some twice — usage + placeholder).
        # Plus pre-existing /skip /exec-retry /logs /cost references.
        assert src.count("01ab243e") >= 8, (
            "§17.301 regression: the canonical `01ab243e` example "
            "fragment is no longer used consistently across job_id-"
            "taking commands. Future fixes should mirror the same "
            "example so operators learn one canonical id, not many."
        )

    def test_jobs_hint_present_in_each_usage(self):
        """Every §17.301 missing-arg message includes the `/jobs`
        hint so operators discover the lookup command."""
        src = self._src()
        # The hint phrase — anchor for the "💡 Use `/jobs`" pattern.
        # Count is ≥ 5 (one per command) — exact count is fragile
        # because /jobs rename + delete may have multiple hint sites.
        assert src.count("`/jobs`") >= 5, (
            "§17.301 regression: the `/jobs` discovery hint is no "
            "longer present in enough usage messages. Each job_id-"
            "taking command's usage line should point operators at "
            "`/jobs` so they know how to find a real id."
        )
