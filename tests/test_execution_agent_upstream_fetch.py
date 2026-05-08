"""Sprint X.4 — _fetch_upstream_outputs try/except wrap.

Same shape as W.4 (prompt-build wrap), one phase earlier. A DB-layer failure
in _fetch_upstream_outputs (asyncpg connection drop, deadlock, etc.) must
mark the node 'failed' with last_verification_reason populated, so W.1's
retry-feedback loop has something to learn from on the next /exec/retry.

Without this wrap, the exception propagates to execute_all_nodes' generic
handler, which forces the node 'failed' via raw SQL but leaves
last_verification_reason NULL — defeating W.1.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _build_session_mock(db_mock):
    """Build an async_session() factory that returns the given AsyncMock db."""
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=db_mock)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


@pytest.mark.smoke
class TestUpstreamFetchFailureContract:
    """Failure during _fetch_upstream_outputs → 'failed' status + persisted reason."""

    async def test_fetch_upstream_raise_returns_failed_dict(self):
        """_fetch_upstream_outputs raising → execute_next_node returns the
        failed-shape dict with reason='upstream_fetch_error', not letting
        the exception bubble to execute_all_nodes' generic handler."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {"description": "x"},
        })
        # depends_on must be non-empty so the upstream-fetch path actually fires.
        mock_get_next = AsyncMock(return_value={
            "id": "node-1",
            "node_key": "T2",
            "title": "Build something downstream",
            "tool": "LLM",
            "prompt_template": "do X",
            "domain": None,
            "depends_on": ["T1"],
            "assigned_model": None,
            "retry_count": 0,
            "last_verification_reason": None,
        })
        mock_fetch = AsyncMock(side_effect=RuntimeError("asyncpg connection lost"))
        mock_set_status = AsyncMock()
        mock_log_exec = AsyncMock()

        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._fetch_upstream_outputs",
                   new=mock_fetch), \
             patch("app.modules.execution_agent._set_node_status",
                   new=mock_set_status), \
             patch("app.modules.execution_agent._log_execution",
                   new=mock_log_exec):
            result = await execute_next_node("job-1")

        assert result["status"] == "failed"
        assert result["node_key"] == "T2"
        assert result["title"] == "Build something downstream"
        assert "upstream fetch error" in result["error"]
        assert "asyncpg connection lost" in result["error"]
        assert result["reason"] == "upstream_fetch_error"
        # Same dict contract as W.4 / timeout / exec-error paths.
        assert "message" in result
        assert "verification_reason" in result
        assert "upstream fetch error" in result["verification_reason"]

    async def test_set_node_status_called_with_verification_reason(self):
        """The W.1 retry-feedback loop depends on last_verification_reason
        being persisted. X.4 must call _set_node_status with the reason set."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {"description": "x"},
        })
        mock_get_next = AsyncMock(return_value={
            "id": "node-1", "node_key": "T2", "title": "X", "tool": "LLM",
            "prompt_template": None, "domain": None, "depends_on": ["T1"],
            "assigned_model": None, "retry_count": 0,
            "last_verification_reason": None,
        })
        mock_fetch = AsyncMock(
            side_effect=ConnectionError("postgres terminated unexpectedly"),
        )
        mock_set_status = AsyncMock()

        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._fetch_upstream_outputs",
                   new=mock_fetch), \
             patch("app.modules.execution_agent._set_node_status",
                   new=mock_set_status), \
             patch("app.modules.execution_agent._log_execution",
                   new=AsyncMock()):
            await execute_next_node("job-1")

        # _set_node_status called once for the failure path.
        assert mock_set_status.call_count == 1
        call = mock_set_status.call_args
        # Positional: (db, node_id, status); keyword: verification_reason
        assert call.args[2] == "failed"
        assert "verification_reason" in call.kwargs
        assert "upstream fetch error" in call.kwargs["verification_reason"]
        # The underlying error message survives in the persisted reason.
        assert "postgres terminated" in call.kwargs["verification_reason"]


@pytest.mark.smoke
class TestUpstreamFetchHappyPathUnaffected:
    """The X.4 wrap must not change the no-deps or happy-fetch paths."""

    async def test_no_depends_on_skips_fetch_entirely(self):
        """When depends_on is empty, _fetch_upstream_outputs is NOT called.
        The wrap exists but the inner conditional bypasses it. Regression
        guard against accidentally fetching for leaf nodes."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {"description": "x"},
        })
        mock_get_next = AsyncMock(return_value={
            "id": "node-1", "node_key": "T1", "title": "X", "tool": "LLM",
            "prompt_template": "do X", "domain": None, "depends_on": [],
            "assigned_model": None, "retry_count": 0,
            "last_verification_reason": None,
        })
        # If _fetch_upstream_outputs is called despite depends_on=[], this
        # AsyncMock records it. The contract is: zero calls.
        mock_fetch = AsyncMock(return_value={})
        # Force _build_prompt to raise so we don't try to dispatch a real LLM.
        # The test only cares about whether the upstream fetch was invoked.
        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._fetch_upstream_outputs",
                   new=mock_fetch), \
             patch("app.modules.execution_agent._build_prompt",
                   side_effect=RuntimeError("stop here, not the point")), \
             patch("app.modules.execution_agent._set_node_status",
                   new=AsyncMock()), \
             patch("app.modules.execution_agent._log_execution",
                   new=AsyncMock()):
            await execute_next_node("job-1")

        assert mock_fetch.call_count == 0

    async def test_w4_path_not_used_for_upstream_failures(self):
        """An upstream-fetch failure must surface as reason='upstream_fetch_error',
        NOT the W.4 prompt_build_error path. Confirms the two wraps are
        independent — if X.4 ever silently regresses to letting the exception
        propagate into the W.4 wrap, this test catches it."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {"description": "x"},
        })
        mock_get_next = AsyncMock(return_value={
            "id": "node-1", "node_key": "T2", "title": "X", "tool": "LLM",
            "prompt_template": "do X", "domain": None, "depends_on": ["T1"],
            "assigned_model": None, "retry_count": 0,
            "last_verification_reason": None,
        })
        mock_fetch = AsyncMock(side_effect=RuntimeError("db pool exhausted"))

        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._fetch_upstream_outputs",
                   new=mock_fetch), \
             patch("app.modules.execution_agent._set_node_status",
                   new=AsyncMock()), \
             patch("app.modules.execution_agent._log_execution",
                   new=AsyncMock()):
            result = await execute_next_node("job-1")

        assert result["status"] == "failed"
        assert result["reason"] == "upstream_fetch_error"
        assert result["reason"] != "prompt_build_error"
