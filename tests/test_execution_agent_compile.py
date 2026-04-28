"""Tests for execution_agent — _compile_output 3-strategy priority chain + partial/cache/marker variants.

Split from the original test_execution_agent.py (#9.6). Shared imports
and helpers live in _execution_agent_shared.
"""
from tests._execution_agent_shared import *  # noqa: F401, F403

@pytest.mark.smoke
class TestCompileOutputStrategy2:
    """Strategy 2: last CodeGen terminal node is the deliverable."""

    async def test_last_codegen_selected(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan text"},
            {"node_key": "T2", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "def hello(): pass"},
            {"node_key": "T3", "title": "Refactor", "tool": "CodeGen",
             "status": "done", "output_text": "def hello():\n    print('hi')"},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert result == "def hello():\n    print('hi')"

    async def test_codegen_not_last_falls_through(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "code here"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM",
             "status": "done", "output_text": "summary here"},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert "## T1:" in result
        assert "## T2:" in result


@pytest.mark.smoke
class TestCompileOutputStrategy3:
    """Strategy 3: concatenate all passed outputs with headers."""

    async def test_concatenation_format(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "SearXNG",
             "status": "done", "output_text": "research data"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "done", "output_text": "analysis results"},
            {"node_key": "T3", "title": "Review", "tool": "LLM",
             "status": "done", "output_text": "review notes"},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert "## T1: Research" in result
        assert "## T2: Analyze" in result
        assert "## T3: Review" in result
        assert "---" in result

    async def test_failed_nodes_excluded(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "good stuff"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T3", "title": "Blocked task", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert "## T1: Research" in result
        assert "T2" not in result
        assert "T3" not in result

    async def test_no_done_nodes_returns_empty(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert result == ""


@pytest.mark.smoke
class TestCompileOutputPartial:
    """Partial compile behavior — _compile_output returns what it can;
    caller adds [PARTIAL] prefix."""

    async def test_partial_with_some_done(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "Milvus",
             "status": "done", "output_text": "kb results"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T3", "title": "Compare", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert "kb results" in result
        assert "T2" not in result
        partial = "[PARTIAL — some nodes failed or blocked]\n\n" + result
        assert partial.startswith("[PARTIAL")

    async def test_all_failed_returns_empty_not_none(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "failed", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert result is not None
        assert isinstance(result, str)
        assert result == ""


@pytest.mark.smoke
class TestCompileOutputCache:
    """#22: blocked job with cached compiled_output skips recompute."""

    async def test_cached_compiled_output_skips_recompute(self):
        """When jobs.compiled_output is populated, _compile_output is NOT called."""
        from app.modules.execution_agent import execute_next_node

        cached_result = MagicMock()
        cached_result.scalar.return_value = "CACHED COMPILED OUTPUT"
        status_update_result = MagicMock()
        blocked_query_result = MagicMock()
        blocked_query_result.fetchall.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            cached_result,         # SELECT compiled_output -> cache hit
            status_update_result,  # UPDATE status = 'blocked' (idempotent guard)
            blocked_query_result,  # SELECT for blocked_nodes detail
        ])
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {}
        })
        mock_get_next = AsyncMock(return_value=None)
        mock_all_done = AsyncMock(return_value=False)
        mock_compile = AsyncMock()  # MUST NOT be called

        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("app.modules.execution_agent.async_session", mock_session_factory), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._all_nodes_done", mock_all_done), \
             patch("app.modules.execution_agent._compile_output", mock_compile):
            result = await execute_next_node("job-1")

        assert result["status"] == "blocked"
        mock_compile.assert_not_called()

    async def test_uncached_blocked_job_recomputes(self):
        """When compiled_output is NULL, _compile_output IS called and result stored."""
        from app.modules.execution_agent import execute_next_node

        cached_result = MagicMock()
        cached_result.scalar.return_value = None  # cache miss
        status_update_result = MagicMock()
        blocked_query_result = MagicMock()
        blocked_query_result.fetchall.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            cached_result,         # SELECT compiled_output -> None
            status_update_result,  # UPDATE compiled_output + status
            blocked_query_result,  # SELECT for blocked_nodes detail
        ])
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {}
        })
        mock_get_next = AsyncMock(return_value=None)
        mock_all_done = AsyncMock(return_value=False)
        mock_compile = AsyncMock(return_value="FRESHLY COMPILED")

        mock_session_factory = MagicMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch("app.modules.execution_agent.async_session", mock_session_factory), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._all_nodes_done", mock_all_done), \
             patch("app.modules.execution_agent._compile_output", mock_compile):
            result = await execute_next_node("job-1")

        assert result["status"] == "blocked"
        mock_compile.assert_called_once()


@pytest.mark.smoke
class TestCompileOutputExplicitMarker:
    """#97: is_output_node=TRUE takes precedence over title heuristics."""

    async def test_is_output_node_overrides_title_heuristic(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research topic", "tool": "SearXNG",
             "status": "done", "output_text": "should NOT be used",
             "is_output_node": False},
            {"node_key": "T2", "title": "Summarize output", "tool": "LLM",
             "status": "done", "output_text": "legacy heuristic winner",
             "is_output_node": False},
            {"node_key": "T3", "title": "Final deliverable", "tool": "LLM",
             "status": "done", "output_text": "EXPLICIT WINNER",
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert result == "EXPLICIT WINNER"
    async def test_explicit_marker_but_not_done_falls_through(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "fallback content",
             "is_output_node": False},
            {"node_key": "T2", "title": "Compose output", "tool": "LLM",
             "status": "failed", "output_text": None,
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert "fallback content" in result
        assert "T2" not in result

    async def test_multiple_output_nodes_joined(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Part A", "tool": "LLM",
             "status": "done", "output_text": "alpha",
             "is_output_node": True},
            {"node_key": "T2", "title": "Part B", "tool": "LLM",
             "status": "done", "output_text": "beta",
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result = await _compile_output("job-1", db)
        assert "alpha" in result
        assert "beta" in result
        assert "---" in result
