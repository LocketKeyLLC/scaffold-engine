"""§17.304 — post-execution Next-block mirrors §17.303 at the journey END.

Pre-§17.304 the `/execute/all` (and the /confirm auto-chain that
ends there) yielded the compiled output and then either nothing or
`Use \\`/results <job_id>\\` for details` as the sole signpost.
Operators with a result in hand had no copy-pasteable path to /cost,
/results, or — critically — /exec retry on failed nodes.

§17.304 introduces `_render_completion_next_block(job_id, failed_nodes)`
appended to every terminal yield in `_execute_and_stream`:

  Happy path (no failures):
    - /results <id> — full status + node-by-node detail
    - /cost <id> — see total LLM cost + latency rollup
    - /jobs rename <id> <new title> — set a memorable title

  Partial / failed path (per failed_node):
    - /exec retry <id> <node_key> ("title") — retry this failed step
    - /results <id> — full status
    - /cost <id> — costs
    (rename suggestion omitted — fix first, tune later)

These tests pin both shapes + the renderer's source-shape anchor.
"""
import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


_SAMPLE_JOB_ID = "abc1234e-d5f6-7890-abcd-ef1234567890"


@pytest.mark.smoke
class TestCompletionNextBlockHappyPath:
    """§17.304 — no failures: surface results / cost / rename."""

    def test_results_cost_rename_all_present(self, pipe):
        out = pipe._render_completion_next_block(_SAMPLE_JOB_ID, [])
        assert "Next steps:" in out
        # All three commands pre-filled with the real id.
        assert f"/results {_SAMPLE_JOB_ID}" in out
        assert f"/cost {_SAMPLE_JOB_ID}" in out
        assert f"/jobs rename {_SAMPLE_JOB_ID}" in out

    def test_no_exec_retry_when_no_failures(self, pipe):
        """`/exec retry` is operator-action-on-failure; suppress it
        on the happy path to keep the menu short."""
        out = pipe._render_completion_next_block(_SAMPLE_JOB_ID, [])
        assert "/exec retry" not in out

    def test_pre_fills_real_id_not_placeholder(self, pipe):
        """Mirror of §17.303's anchor: the actual id appears in every
        command, never the literal `<job_id>` placeholder."""
        out = pipe._render_completion_next_block(_SAMPLE_JOB_ID, [])
        assert "<job_id>" not in out


@pytest.mark.smoke
class TestCompletionNextBlockPartialPath:
    """§17.304 — failed_nodes present: /exec retry rows for each."""

    def test_exec_retry_row_per_failed_node(self, pipe):
        failed = [
            {"node_key": "T3", "title": "Summarize findings"},
            {"node_key": "T7", "title": "Generate report"},
        ]
        out = pipe._render_completion_next_block(_SAMPLE_JOB_ID, failed)
        assert f"/exec retry {_SAMPLE_JOB_ID} T3" in out
        assert f"/exec retry {_SAMPLE_JOB_ID} T7" in out
        # Titles surfaced as quoted suffixes for visual disambiguation.
        assert "\"Summarize findings\"" in out
        assert "\"Generate report\"" in out

    def test_exec_retry_rows_appear_before_results_cost(self, pipe):
        """Operator-action priority: surface the actionable retry
        rows above the inspection commands."""
        failed = [{"node_key": "T3", "title": "x"}]
        out = pipe._render_completion_next_block(_SAMPLE_JOB_ID, failed)
        retry_idx = out.index("/exec retry")
        results_idx = out.index("/results")
        assert retry_idx < results_idx

    def test_rename_suggestion_suppressed_on_partial(self, pipe):
        """Operator decision tree when there are failures is 'fix
        first, tune later' — don't suggest renaming a half-done job."""
        failed = [{"node_key": "T3"}]
        out = pipe._render_completion_next_block(_SAMPLE_JOB_ID, failed)
        assert "/jobs rename" not in out

    def test_failed_node_without_title_still_renders(self, pipe):
        """Title is optional — node_key alone must still produce a
        copy-pasteable command without a malformed suffix."""
        failed = [{"node_key": "T3"}]
        out = pipe._render_completion_next_block(_SAMPLE_JOB_ID, failed)
        assert f"/exec retry {_SAMPLE_JOB_ID} T3" in out
        # No empty quotes when title is missing.
        assert "\"\"" not in out

    def test_non_dict_failed_node_skipped_silently(self, pipe):
        """Defensive: a misshapen failed_node entry (string instead of
        dict) shouldn't crash the renderer. The contract is dict-shaped
        per execute_all_nodes' SSE emission; non-dicts are skipped."""
        out = pipe._render_completion_next_block(
            _SAMPLE_JOB_ID, ["some-broken-string"],
        )
        # Just /results + /cost (no retry rows, no rename).
        assert "/exec retry" not in out
        assert "/results" in out and "/cost" in out


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:

    def _src(self) -> str:
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            return f.read()

    def test_renderer_method_anchored(self):
        src = self._src()
        assert "def _render_completion_next_block" in src, (
            "§17.304 regression: the post-execution Next-block renderer "
            "has been removed. /execute/all completion goes back to "
            "yielding just compiled output + the bare /results hint."
        )

    def test_renderer_called_after_compiled_output(self):
        """Pin that the renderer is invoked at every terminal yield
        in _execute_and_stream — both the happy path (compiled_output
        present) and the four fallback branches."""
        src = self._src()
        # ≥ 5 call sites: 1 happy + 4 fallbacks (compiled_output
        # available, success fallback, non-200, exception path,
        # and the fetch-status branch).
        count = src.count("self._render_completion_next_block(job_id")
        assert count >= 5, (
            f"§17.304 regression: only {count} call sites invoke the "
            f"completion Next-block. Expected ≥ 5 — one per terminal "
            f"yield in _execute_and_stream. Operators on a fallback "
            f"branch would lose the Next-block while happy-path "
            f"operators see it."
        )

    def test_exec_retry_template_anchored(self):
        """The /exec retry template that pre-fills both job_id and
        node_key — load-bearing for the partial-failure UX."""
        src = self._src()
        assert "/exec retry {job_id} {nk}" in src, (
            "§17.304 regression: the /exec retry template no longer "
            "pre-fills both job_id and node_key. Operators on a "
            "partial-failure result can't copy-paste the retry."
        )
