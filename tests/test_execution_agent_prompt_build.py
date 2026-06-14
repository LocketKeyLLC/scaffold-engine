"""Sprint W.4 — prompt-build try/except wrap.

Verifies that an exception during prompt assembly (build → RAG/upstream/
optimize) marks the node 'failed' with last_verification_reason populated,
and returns the same dict contract as the timeout/exec-error paths.
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
class TestPromptBuildFailureContract:
    """Failure during prompt assembly → 'failed' status + persisted reason."""

    async def test_build_prompt_raise_returns_failed_dict(self):
        """_build_prompt raising a TypeError → execute_next_node returns
        the failed-shape dict instead of letting the exception bubble."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {"description": "x"},
        })
        # Real-ish node so we get past the human/skip short-circuit and reach
        # the prompt-build phase. depends_on=[] avoids the upstream-fetch path.
        mock_get_next = AsyncMock(return_value={
            "id": "node-1",
            "node_key": "T1",
            "title": "Build something",
            "tool": "LLM",
            "prompt_template": "do X",
            "domain": None,
            "depends_on": [],
            "assigned_model": None,
            "retry_count": 0,
            "last_verification_reason": None,
        })
        mock_set_status = AsyncMock()
        mock_log_exec = AsyncMock()

        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._build_prompt",
                   side_effect=TypeError("malformed snapshot")), \
             patch("app.modules.execution_agent._set_node_status",
                   new=mock_set_status), \
             patch("app.modules.execution_agent._log_execution",
                   new=mock_log_exec):
            result = await execute_next_node("job-1")

        assert result["status"] == "failed"
        assert result["node_key"] == "T1"
        assert result["title"] == "Build something"
        assert "prompt build error" in result["error"]
        assert "malformed snapshot" in result["error"]
        assert result["reason"] == "prompt_build_error"
        # Same shape as the existing exec-error contract: status, node_key,
        # title, error, message — plus W.4 adds verification_reason for
        # downstream feedback consumers.
        assert "message" in result
        assert "verification_reason" in result
        assert "prompt build error" in result["verification_reason"]

    async def test_set_node_status_called_with_verification_reason(self):
        """The W.1 retry-feedback loop depends on last_verification_reason being
        persisted. W.4 must call _set_node_status with verification_reason set
        to a meaningful string."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {"description": "x"},
        })
        mock_get_next = AsyncMock(return_value={
            "id": "node-1", "node_key": "T1", "title": "X", "tool": "LLM",
            "prompt_template": None, "domain": None, "depends_on": [],
            "assigned_model": None, "retry_count": 0,
            "last_verification_reason": None,
        })
        mock_set_status = AsyncMock()

        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._build_prompt",
                   side_effect=KeyError("title")), \
             patch("app.modules.execution_agent._set_node_status",
                   new=mock_set_status), \
             patch("app.modules.execution_agent._log_execution",
                   new=AsyncMock()):
            await execute_next_node("job-1")

        # _set_node_status was called once for the failure path.
        assert mock_set_status.call_count == 1
        # Inspect the kwargs: must have verification_reason set with the build error.
        call = mock_set_status.call_args
        # Positional: (db, node_id, status); keyword: verification_reason
        assert call.args[2] == "failed"
        assert "verification_reason" in call.kwargs
        assert "prompt build error" in call.kwargs["verification_reason"]
        assert "title" in call.kwargs["verification_reason"]  # the KeyError content

    async def test_rag_fetch_failure_caught_by_outer_wrap(self):
        """If a helper used during prompt assembly raises (here:
        _fetch_rag_context, since the more typical failure mode), the W.4
        outer wrap catches it. Covers the case where helpers don't have their
        own internal try/except (they currently do, but contracts shift —
        the outer wrap is the safety net).

        We patch _fetch_rag_context to raise rather than its real behavior
        of returning '' — proves the outer wrap is the backstop, not just
        _build_prompt failures."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running",
            "refined_brief": {"description": "x", "goals": ["y"]},
        })
        mock_get_next = AsyncMock(return_value={
            "id": "node-1", "node_key": "T1", "title": "X", "tool": "LLM",
            "prompt_template": "do X", "domain": None, "depends_on": [],
            "assigned_model": None, "retry_count": 0,
            "last_verification_reason": None,
        })
        mock_set_status = AsyncMock()

        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._fetch_rag_context",
                   side_effect=RuntimeError("milvus pool exhausted")), \
             patch("app.modules.execution_agent._set_node_status",
                   new=mock_set_status), \
             patch("app.modules.execution_agent._log_execution",
                   new=AsyncMock()):
            result = await execute_next_node("job-1")

        assert result["status"] == "failed"
        assert "milvus pool exhausted" in result["error"]
        assert "milvus pool exhausted" in result["verification_reason"]
        # Confirm the failed-node DB write happened
        assert mock_set_status.call_count == 1


@pytest.mark.smoke
class TestPromptBuildSuccessUnaffected:
    """The W.4 wrap must not change the happy path — successful prompt
    assembly proceeds to LLM execution as before."""

    async def test_clean_build_then_llm_failure_uses_exec_error_path(self):
        """Prompt build succeeds → LLM dispatch fails → falls through to the
        existing exec-error handler (line 708), NOT the W.4 prompt_build_error
        path. Proves the W.4 wrap is scoped only to the assembly phase."""
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running",
            "refined_brief": {"description": "x"},
        })
        mock_get_next = AsyncMock(return_value={
            "id": "node-1", "node_key": "T1", "title": "X", "tool": "LLM",
            "prompt_template": "do X", "domain": None, "depends_on": [],
            "assigned_model": None, "retry_count": 0,
            "last_verification_reason": None,
        })
        mock_set_status = AsyncMock()

        # Force LLM dispatch to fail AFTER the prompt-build phase succeeded.
        mock_chat = AsyncMock(side_effect=RuntimeError("ollama refused"))

        with patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent.model_router.chat",
                   new=mock_chat), \
             patch("app.modules.execution_agent._set_node_status",
                   new=mock_set_status), \
             patch("app.modules.execution_agent.optimize_prompt",
                   new=AsyncMock(side_effect=Exception("bypass optimize"))), \
             patch("app.modules.execution_agent._fetch_rag_context",
                   new=AsyncMock(return_value="")), \
             patch("app.modules.execution_agent._log_execution",
                   new=AsyncMock()):
            result = await execute_next_node("job-1")

        assert result["status"] == "failed"
        # Crucial: the failure was NOT caught by the W.4 wrap (otherwise
        # reason would be 'prompt_build_error'). It was caught by the
        # existing exec-error handler.
        assert result.get("reason") != "prompt_build_error"
        assert "ollama refused" in result["error"]
        # And _set_node_status got the LLM-error verification_reason, not
        # the W.4 prompt-build-error string.
        assert mock_set_status.call_count == 1
        reason = mock_set_status.call_args.kwargs.get("verification_reason", "")
        assert "execution error" in reason
        assert "prompt build error" not in reason


@pytest.mark.smoke
class TestGroundingDomainFanout:
    """§17.517 — general node grounding fans out across ALL domains by default
    (domain=None) so `/research` ingested under a different (heuristic) partition
    than the job's domain is still found; the setting restores job-scoping."""

    @staticmethod
    async def _capture_grounding_domain(cross_domain: bool):
        from app.modules import execution_agent
        from app.modules.execution_agent import execute_next_node

        db = AsyncMock(); db.execute = AsyncMock(); db.commit = AsyncMock()
        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running",
            "refined_brief": {"description": "x", "goals": ["y"], "domain": "eng"},
        })
        mock_get_next = AsyncMock(return_value={
            "id": "node-1", "node_key": "T1", "title": "X", "tool": "LLM",
            "prompt_template": "do X", "domain": None, "depends_on": [],
            "assigned_model": None, "retry_count": 0,
            "last_verification_reason": None,
        })
        rag = AsyncMock(return_value="")  # recording mock; grounding "succeeds" empty
        # Patch on the object execute_next_node reads (its module global), so a
        # reloaded app.config can't decouple it (cf. §17.513).
        with patch.object(execution_agent.settings,
                          "execution_grounding_cross_domain", cross_domain), \
             patch("app.modules.execution_agent.async_session",
                   _build_session_mock(db)), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._fetch_rag_context", new=rag), \
             patch("app.modules.execution_agent.optimize_prompt",
                   new=AsyncMock(side_effect=Exception("bypass optimize"))), \
             patch("app.modules.execution_agent.model_router.chat",
                   new=AsyncMock(side_effect=RuntimeError("stop after grounding"))), \
             patch("app.modules.execution_agent._set_node_status", new=AsyncMock()), \
             patch("app.modules.execution_agent._log_execution", new=AsyncMock()):
            await execute_next_node("job-1")
        assert rag.called, "grounding must run for an LLM node"
        return rag.call_args.kwargs.get("domain", "MISSING")

    async def test_default_fans_out_all_domains(self):
        # job domain is "eng", but cross-domain default → grounding searches ALL
        assert await self._capture_grounding_domain(True) is None

    async def test_setting_false_scopes_to_job_domain(self):
        assert await self._capture_grounding_domain(False) == "eng"
