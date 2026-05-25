"""§17.299 — execution_retry module + alias contract.

§17.280-🟢-4 closeout. The retry helpers (``_format_reviewer_feedback``
and ``retry_failed_node``) moved from ``app/modules/execution_agent.py``
into ``app/modules/execution_retry.py``. ``execution_agent`` re-imports
them under their original names so existing call sites + tests keep
working byte-for-byte.

These tests pin:

  1. ``execution_retry`` exports both names.
  2. ``execution_agent``'s exposed names ARE the vendor module's
     functions (identity, not equality — catches a re-inlined body).
  3. The pre-§17.299 inline function bodies are absent from
     ``execution_agent.py`` source.
  4. The original ``execute_all_nodes`` auto-retry plumbing (which
     uses ``retry_failed_node`` inline) still references the name
     via the re-import — so a future refactor that drops the
     re-export would surface here.
"""
from __future__ import annotations

import inspect

import pytest


@pytest.mark.smoke
class TestExecutionRetryExports:
    """§17.299 — the new module's public surface."""

    def test_module_imports(self):
        from app.modules import execution_retry
        assert hasattr(execution_retry, "_format_reviewer_feedback")
        assert hasattr(execution_retry, "retry_failed_node")

    def test_format_reviewer_feedback_is_sync(self):
        """``_format_reviewer_feedback`` is called from the synchronous
        ``_build_prompt`` path in execution_agent — it must NOT be
        coroutine."""
        from app.modules.execution_retry import _format_reviewer_feedback
        assert not inspect.iscoroutinefunction(_format_reviewer_feedback)

    def test_retry_failed_node_is_async(self):
        """``retry_failed_node`` is awaited from both the /exec/retry
        endpoint and the auto-retry budget loop — must be async."""
        from app.modules.execution_retry import retry_failed_node
        assert inspect.iscoroutinefunction(retry_failed_node)


@pytest.mark.smoke
class TestExecutionAgentAliasIdentity:
    """§17.299 — execution_agent's re-imports point at the vendor."""

    def test_format_reviewer_feedback_alias_is_vendor(self):
        from app.modules import execution_agent, execution_retry
        assert (
            execution_agent._format_reviewer_feedback
            is execution_retry._format_reviewer_feedback
        ), (
            "§17.299 regression: execution_agent._format_reviewer_feedback "
            "is no longer the execution_retry vendor function. Either the "
            "re-import was removed or a duplicate body was re-inlined. "
            "Restore the re-import or remove the duplicate."
        )

    def test_retry_failed_node_alias_is_vendor(self):
        from app.modules import execution_agent, execution_retry
        assert (
            execution_agent.retry_failed_node
            is execution_retry.retry_failed_node
        ), (
            "§17.299 regression: execution_agent.retry_failed_node is no "
            "longer the execution_retry vendor function. The /exec/retry "
            "router calls it via execution_agent; identity drift here "
            "means tests patching execution_agent.retry_failed_node "
            "would NOT affect the auto-retry budget loop."
        )


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.299 — execution_agent.py no longer carries the lifted bodies."""

    def _src(self) -> str:
        from app.modules import execution_agent
        with open(execution_agent.__file__, encoding="utf-8") as f:
            return f.read()

    def test_no_inline_format_reviewer_feedback_body(self):
        """The unique first line of the pre-§17.299 inline body. After
        the lift, this string appears ONLY in execution_retry.py — not
        in execution_agent.py."""
        src = self._src()
        # The function's docstring opener is unique to the body —
        # research_agent / other modules don't use this exact line.
        assert "Return a Reviewer-feedback block to prepend on retry" not in src, (
            "§17.299 regression: the inline `_format_reviewer_feedback` "
            "body has reappeared in execution_agent.py. The function "
            "must live in execution_retry.py only; execution_agent "
            "imports it back via the re-export at the audit citation."
        )

    def test_no_inline_retry_failed_node_body(self):
        """The unique BFS+Stage-N comment skeleton from the inline
        body. Stage 1-7 comments are distinctive to retry_failed_node's
        original implementation."""
        src = self._src()
        # Two anchors that together appeared ONLY in the inline body.
        # Either anchor alone could plausibly appear elsewhere; the
        # AND combination is the regression signature.
        has_stages = "# ---- Stage 4: BFS for transitive downstream nodes ----" in src
        has_bfs_helper = "downstream_map: dict[str, set[str]] = {}" in src
        assert not (has_stages and has_bfs_helper), (
            "§17.299 regression: the inline `retry_failed_node` body "
            "(identified by the Stage-4 BFS comment + the downstream_map "
            "assignment together) has reappeared in execution_agent.py. "
            "Restore the import from execution_retry."
        )

    def test_reimport_lines_present(self):
        """Pin that the re-export lines stay anchored in source so a
        future contributor reading the dispatch can trace back."""
        src = self._src()
        assert "from app.modules.execution_retry import _format_reviewer_feedback" in src, (
            "§17.299: the _format_reviewer_feedback re-import is missing "
            "from execution_agent.py. _build_prompt's call site won't "
            "resolve."
        )
        assert "from app.modules.execution_retry import retry_failed_node" in src, (
            "§17.299: the retry_failed_node re-import is missing from "
            "execution_agent.py. The auto-retry budget loop + the "
            "/exec/retry router call site won't resolve."
        )
