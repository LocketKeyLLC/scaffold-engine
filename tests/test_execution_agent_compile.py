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
        result, _was_syn = await _compile_output("job-1", db)
        assert result == "def hello():\n    print('hi')"

    async def test_codegen_not_last_falls_through(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "code here"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM",
             "status": "done", "output_text": "summary here"},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
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
        result, _was_syn = await _compile_output("job-1", db)
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
        result, _was_syn = await _compile_output("job-1", db)
        assert "## T1: Research" in result
        assert "T2" not in result
        assert "T3" not in result

    async def test_no_done_nodes_returns_none(self):
        """W.2: returns None (not "") so caller stores NULL — semantically
        cleaner than an empty string ("we never produced output" vs. "we
        produced an empty string")."""
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is None


@pytest.mark.smoke
class TestCompileOutputW2Strategy3:
    """W.2 additions: preamble + truncation + diagnostic warning."""

    async def test_strategy3_prepends_partial_deliverable_preamble(self):
        """When Strategy 3 fires (no leaf done, last not CodeGen), the
        output starts with a 'Partial deliverable' banner so consumers
        can tell this is a fallback rather than a clean Strategy-0 result."""
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "research data"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "done", "output_text": "analysis"},
            # T3 is the leaf but it's still pending → no Strategy 0 hit
            {"node_key": "T3", "title": "Synthesize", "tool": "LLM",
             "status": "pending", "output_text": None,
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is not None
        assert "Partial deliverable" in result
        assert "2 of 3" in result
        # And the actual section content still appears below the preamble
        assert "## T1: Research" in result

    async def test_strategy3_truncates_when_total_exceeds_cap(self, monkeypatch):
        """Pathological case: many verbose nodes blow past the storage cap.
        Each section is truncated proportionally so the artifact stays
        readable rather than ballooning into a multi-MB blob."""
        from app.config import settings
        monkeypatch.setattr(settings, "compile_output_max_chars", 2_000)

        big = "x" * 5_000
        db = make_mock_db([
            {"node_key": f"T{i}", "title": f"Node {i}", "tool": "LLM",
             "status": "done", "output_text": big}
            for i in range(1, 5)
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is not None
        # Truncation marker should appear in each section.
        assert result.count("[...truncated") >= 1
        # Result still includes a section per node.
        for i in range(1, 5):
            assert f"## T{i}: Node {i}" in result

    async def test_strategy3_logs_warning_with_done_nodes(self, caplog):
        """Diagnostic: Strategy 3 firing with done output is a hint that
        the dag_generator's leaf-set logic missed this DAG shape (or the
        true leaves failed). Warning is the trail teams follow."""
        import logging
        caplog.set_level(logging.WARNING, logger="scaffold.execution_compile")

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "a"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "done", "output_text": "b"},
        ])
        from app.modules.execution_agent import _compile_output
        await _compile_output("job-1", db)
        assert any(
            "compile_strategy3_fallback" in r.message
            for r in caplog.records
        ), "expected a strategy-3 fallback warning"


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
        result, _was_syn = await _compile_output("job-1", db)
        assert "kb results" in result
        assert "T2" not in result
        partial = "[PARTIAL — some nodes failed or blocked]\n\n" + result
        assert partial.startswith("[PARTIAL")

    async def test_all_failed_returns_none(self):
        """W.2 contract change: no done nodes → return None (was "" pre-W.2).
        Caller stores NULL, which is the semantically correct state."""
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "failed", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result, _was_syn = await _compile_output("job-1", db)
        assert result is None


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
        result, _was_syn = await _compile_output("job-1", db)
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
        result, _was_syn = await _compile_output("job-1", db)
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
        result, _was_syn = await _compile_output("job-1", db)
        assert "alpha" in result
        assert "beta" in result
        assert "---" in result


# ---------------------------------------------------------------------------
# Sprint W.7 — opt-in LLM synthesis pass
# ---------------------------------------------------------------------------


def _synthesis_response(text: str = "synthesized prose"):
    """Build a model_router.tool_call response with a render_summary call."""
    from app.providers.base import ModelResponse, ToolCall
    return ModelResponse(
        text="", model="fake", success=True,
        tool_calls=[ToolCall(
            id="t0", name="render_summary",
            arguments={"summary": text},
        )],
    )


def _synthesis_failure():
    from app.providers.base import ModelResponse
    return ModelResponse(
        text="", model="fake", success=False, error="ollama down",
    )


def _make_db_with_brief(rows, brief_description="Build a parser"):
    """make_mock_db returns a db whose every .execute() yields the same rows.
    For W.7 we also need the brief lookup to return a refined_brief dict.
    Using AsyncMock with side_effect lets us route the second call (brief
    SELECT) to a separate result."""
    from unittest.mock import AsyncMock, MagicMock

    nodes_result = MagicMock()
    nodes_mappings = MagicMock()
    nodes_mappings.all.return_value = rows
    nodes_result.mappings.return_value = nodes_mappings

    brief_result = MagicMock()
    brief_mappings = MagicMock()
    brief_mappings.first.return_value = {
        "refined_brief": {"description": brief_description},
    }
    brief_result.mappings.return_value = brief_mappings

    db = AsyncMock()
    # First call: SELECT dag_nodes ... (compile path)
    # Second call: SELECT refined_brief ... (synthesis path)
    db.execute = AsyncMock(side_effect=[nodes_result, brief_result])
    return db


@pytest.mark.smoke
class TestCompileOutputSynthesis:
    """W.7 — opt-in LLM synthesis. Default OFF; when ON, runs on all
    strategies except CodeGen-source paths (CodeGen guard preserves code)."""

    async def test_synthesis_disabled_returns_heuristic_no_llm_call(self):
        """Default behavior: synthesis off → heuristic returned, no LLM call."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan content"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build content"},
        ])
        synth_mock = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", False), \
             patch(
                 "app.modules.execution_compile._synthesize_compiled_output",
                 new=synth_mock,
             ):
            result, _was_syn = await _compile_output("job-1", db)

        # Strategy 3 heuristic shape
        assert "## T1: Plan" in result
        assert "## T2: Build" in result
        # No synthesis call.
        synth_mock.assert_not_called()

    async def test_synthesis_enabled_strategy3_uses_llm_output(self):
        """Strategy 3 + synthesis ON → LLM-rewritten narrative replaces
        heuristic body."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan content"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build content"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(return_value=_synthesis_response(
                     "Coherent narrative covering both Plan and Build.",
                 )),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        assert result == "Coherent narrative covering both Plan and Build."

    async def test_synthesis_fail_open_returns_heuristic(self):
        """LLM call raises → fall back to heuristic body."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(side_effect=RuntimeError("ollama down")),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        # Heuristic shape preserved.
        assert "## T1: Plan" in result
        assert "## T2: Build" in result

    async def test_synthesis_unsuccessful_response_falls_back(self):
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(return_value=_synthesis_failure()),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        assert "## T1: Plan" in result

    async def test_codegen_guard_skips_synthesis_strategy_2(self):
        """Strategy 2 (last CodeGen): synthesis ON but CodeGen guard fires
        → heuristic returned verbatim, no LLM call."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        code_payload = "def hello():\n    print('hi')"
        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": code_payload},
        ])
        synth_call = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=synth_call,
             ):
            result, _was_syn = await _compile_output("job-1", db)

        # CodeGen output preserved verbatim.
        assert result == code_payload
        # CodeGen guard ran inside _synthesize_compiled_output before the
        # LLM call would have fired.
        synth_call.assert_not_called()

    async def test_synthesis_enabled_strategy0_single_leaf(self):
        """Strategy 0 single-leaf with non-CodeGen tool → synthesis fires."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "raw research dump"},
            {"node_key": "T2", "title": "Synthesize", "tool": "LLM",
             "status": "done", "output_text": "explicit leaf body",
             "is_output_node": True},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=AsyncMock(return_value=_synthesis_response(
                     "Polished narrative version.",
                 )),
             ):
            result, _was_syn = await _compile_output("job-1", db)

        assert result == "Polished narrative version."

    async def test_synthesis_strategy0_codegen_leaf_skipped(self):
        """Strategy 0 single-leaf with tool=CodeGen → guard fires, raw code returned."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        code = "fn main() { println!(\"hi\"); }"
        db = make_mock_db([
            {"node_key": "T1", "title": "Write the binary", "tool": "CodeGen",
             "status": "done", "output_text": code,
             "is_output_node": True},
        ])
        synth_call = AsyncMock()
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch(
                 "app.model_router.tool_call",
                 new=synth_call,
             ):
            result, _was_syn = await _compile_output("job-1", db)
        assert result == code
        synth_call.assert_not_called()


# ---------------------------------------------------------------------------
# Sprint X.2 — synthesized flag + skipped-verify banner
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestCompileOutputSynthesizedFlag:
    """X.2 — _compile_output returns (text, was_synthesized: bool)."""

    async def test_synthesis_disabled_returns_false(self):
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "done", "output_text": "out"},
            {"node_key": "T2", "title": "Y", "tool": "LLM",
             "status": "done", "output_text": "out2"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", False):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is not None
        assert was_syn is False

    async def test_synthesis_enabled_and_succeeded_returns_true(self):
        from app.config import settings
        from app.modules.execution_agent import _compile_output
        from app.providers.base import ModelResponse, ToolCall

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "done", "output_text": "out"},
            {"node_key": "T2", "title": "Y", "tool": "LLM",
             "status": "done", "output_text": "out2"},
        ])
        ok_resp = ModelResponse(
            text="", model="fake", success=True,
            tool_calls=[ToolCall(id="t0", name="render_summary",
                                  arguments={"summary": "Polished narrative."})],
        )
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock(return_value=ok_resp)):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value == "Polished narrative."
        assert was_syn is True

    async def test_synthesis_enabled_but_fail_open_returns_false(self):
        """LLM call fails → heuristic returned + was_synthesized=False so the
        caller persists compiled_output_synthesized=False on jobs."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "done", "output_text": "out"},
            {"node_key": "T2", "title": "Y", "tool": "LLM",
             "status": "done", "output_text": "out2"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock(side_effect=RuntimeError("ollama down"))):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is not None
        assert was_syn is False
        assert "## T1" in text_value  # heuristic shape

    async def test_codegen_guard_returns_false(self):
        """Strategy 2 (last CodeGen) is guarded — synthesis never fires →
        was_synthesized stays False even with synthesis enabled."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan"},
            {"node_key": "T2", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "def hi(): pass"},
        ])
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock()):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value == "def hi(): pass"
        assert was_syn is False

    async def test_empty_result_returns_none_false(self):
        """No done nodes → (None, False) — caller stores NULL + synthesized=False."""
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "X", "tool": "LLM",
             "status": "failed", "output_text": None},
        ])
        text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is None
        assert was_syn is False


@pytest.mark.smoke
class TestSkippedVerifyBanner:
    """X.2 — when N nodes were skipped during execution, prepend an
    operational banner so consumers can tell the deliverable doesn't
    cover the full DAG. Sits AFTER synthesis on the call path so it
    survives any LLM rewriting."""

    async def test_no_skipped_no_banner(self):
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "alpha"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "done", "output_text": "beta"},
        ])
        text_value, _ = await _compile_output("job-1", db)
        assert text_value is not None
        assert "Note:" not in text_value
        assert "skipped" not in text_value.lower()

    async def test_one_skipped_banner_singular(self):
        """skipped_count=1 → singular wording ('1 of 3 task were skipped' would
        be ungrammatical; banner uses 'task' singular)."""
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "alpha"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "done", "output_text": "beta"},
            {"node_key": "T3", "title": "C", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        text_value, _ = await _compile_output("job-1", db)
        assert text_value is not None
        assert text_value.startswith("_Note: 1 of 3 task were skipped")
        # Body still present after the banner
        assert "alpha" in text_value
        assert "beta" in text_value

    async def test_multiple_skipped_banner_plural(self):
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "done", "output_text": "alpha"},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "skipped", "output_text": None},
            {"node_key": "T3", "title": "C", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        text_value, _ = await _compile_output("job-1", db)
        assert text_value is not None
        assert text_value.startswith("_Note: 2 of 3 tasks were skipped")
        assert "alpha" in text_value

    async def test_banner_survives_synthesis(self):
        """Banner is prepended AFTER synthesis, so even if the LLM rewrites
        the heuristic into a clean narrative, the banner stays at the top."""
        from app.config import settings
        from app.modules.execution_agent import _compile_output
        from app.providers.base import ModelResponse, ToolCall

        db = _make_db_with_brief([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan content"},
            {"node_key": "T2", "title": "Build", "tool": "LLM",
             "status": "done", "output_text": "build content"},
            {"node_key": "T3", "title": "Polish", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        ok_resp = ModelResponse(
            text="", model="fake", success=True,
            tool_calls=[ToolCall(id="t0", name="render_summary",
                                  arguments={"summary": "Clean narrative output."})],
        )
        with patch.object(settings, "compile_synthesis_enabled", True), \
             patch("app.model_router.tool_call",
                   new=AsyncMock(return_value=ok_resp)):
            text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is not None
        assert text_value.startswith("_Note: 1 of 3 task were skipped")
        # Synthesized body follows the banner
        assert "Clean narrative output." in text_value
        assert was_syn is True

    async def test_empty_result_no_banner(self):
        """When _compile_output returns None, banner doesn't fire (no text to prepend to)."""
        from app.modules.execution_agent import _compile_output

        db = make_mock_db([
            {"node_key": "T1", "title": "A", "tool": "LLM",
             "status": "skipped", "output_text": None},
            {"node_key": "T2", "title": "B", "tool": "LLM",
             "status": "skipped", "output_text": None},
        ])
        text_value, was_syn = await _compile_output("job-1", db)
        assert text_value is None
        assert was_syn is False
