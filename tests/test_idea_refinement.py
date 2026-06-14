"""
tests/test_idea_refinement.py - Behavioral tests for idea refinement module

Sprint X.11 — refine_idea now uses model_router.tool_call (was .generate
with JSON-coaxing). Tests mock tool_call and feed structured args via
resp.tool_calls[0].arguments instead of resp.text JSON.

Run:  docker exec scaffold-orchestrator pytest tests/test_idea_refinement.py -m smoke --timeout=30 -v
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tests.conftest import make_mock_db


def _run(coro):
    """Run async coroutine synchronously."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_db():
    """Build a mock AsyncSession for refine_idea.

    refine_idea does:
      1. INSERT INTO jobs ... RETURNING id  -> needs scalar_one()
      2. UPDATE jobs SET status='refining'  -> needs execute()
      3. commit()
      4. (after LLM) UPDATE jobs SET ...    -> needs execute()
      5. commit()
    Also _fail_job does UPDATE + commit on failure paths.
    """
    # scalar_one() for the INSERT RETURNING id
    insert_result = MagicMock()
    insert_result.scalar_one.return_value = "job-abc-123"

    # Generic result for UPDATEs
    update_result = MagicMock()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[insert_result, update_result,
                                         update_result, update_result,
                                         update_result, update_result])
    db.commit = AsyncMock()
    return db


def _make_llm_response(success=True, args=None, no_calls=False, error=None):
    """Build a mock LLM response matching model_router.tool_call return shape.

    Sprint X.11:
      - success=True + args={...} → response carries one ToolCall with those args.
      - success=True + no_calls=True → response succeeded but tool_calls is empty
        (the X.11 fail-closed path).
      - success=False → dispatch failure (LLM error / retry exhausted).
    """
    resp = MagicMock()
    resp.success = success
    resp.error = error
    resp.model = "qwen3:4b"
    resp.total_duration_ms = 1234
    resp.text = ""

    if not success or no_calls:
        resp.tool_calls = []
        return resp

    if args is None:
        args = {
            "title": "Test Project",
            "description": "A test project description",
            "goals": ["Goal 1", "Goal 2"],
            "constraints": ["Constraint 1"],
            "domain": "eng",
            "complexity": "medium",
        }
    call = MagicMock()
    call.arguments = args
    resp.tool_calls = [call]
    return resp


# ===========================================================================
# Happy Path
# ===========================================================================

@pytest.mark.smoke
class TestRefineIdeaHappyPath:
    """refine_idea() returns structured output on LLM success."""

    def test_returns_dict_with_job_id(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert isinstance(result, dict)
        assert "job_id" in result
        assert result["job_id"] == "job-abc-123"

    def test_returns_status_awaiting_confirmation(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert result["status"] == "awaiting_confirmation"

    def test_returns_refined_brief(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert "refined_brief" in result
        assert isinstance(result["refined_brief"], dict)
        assert result["refined_brief"]["title"] == "Test Project"

    def test_returns_model_used_and_duration(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert result["model_used"] == "qwen3:4b"
        assert result["duration_ms"] == 1234

    def test_calls_tool_call_with_idea_text(self):
        """Sprint X.11 — idea text must appear in the user message of the
        tool_call payload. Replaces the pre-X.11 check on the first positional
        arg of model_router.generate."""
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            _run(refine_idea("Build a weather app", db))
        kwargs = mock_mr.tool_call.call_args.kwargs
        messages = kwargs["messages"]
        # The user message (last in the list) carries the idea text.
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert any("Build a weather app" in m["content"] for m in user_msgs)


# ===========================================================================
# LLM Failure
# ===========================================================================

@pytest.mark.smoke
class TestRefineIdeaLLMFailure:
    """refine_idea() returns failed status when LLM errors."""

    def test_llm_error_returns_failed(self):
        db = _make_db()
        resp = _make_llm_response(success=False, error="Ollama timeout")
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build something", db))
        assert result["status"] == "failed"
        assert "error" in result

    def test_no_tool_calls_returns_failed(self):
        """Sprint X.11 — the equivalent of the pre-X.11 'unparseable JSON'
        failure mode is now 'tool_call returned no tool_calls'. The
        wrapper handles JSON-coaxing internally; if even that fails, the
        response carries success=True but tool_calls=[] — fail closed."""
        db = _make_db()
        resp = _make_llm_response(no_calls=True)
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build something", db))
        assert result["status"] == "failed"
        assert "error" in result


# ===========================================================================
# Domain Override
# ===========================================================================

@pytest.mark.smoke
class TestRefineIdeaDomainOverride:
    """refine_idea() applies user-supplied domain to the brief."""

    def test_domain_override_applied(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a tool", db, domain="rag"))
        assert result["refined_brief"]["domain"] == "rag"

    def test_no_domain_keeps_llm_value(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a tool", db))
        # LLM response has domain="eng" by default in our mock
        assert result["refined_brief"]["domain"] == "eng"


# ===========================================================================
# Target Status Parameter
# ===========================================================================

@pytest.mark.smoke
class TestRefineIdeaTargetStatus:
    """refine_idea() uses target_status for the final job state."""

    def test_custom_target_status_returned(self):
        """Passing target_status='awaiting_confirmation' must be reflected in the return dict."""
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea(
                "Build a tool", db, target_status="awaiting_confirmation"
            ))
        assert result["status"] == "awaiting_confirmation", (
            "target_status parameter must drive the returned status, not be ignored."
        )

    def test_default_target_status_is_awaiting_confirmation(self):
        """When target_status is not supplied, default 'awaiting_confirmation' must be returned.

        (Default changed from 'planning' so /ideas-created jobs no longer
        orphan in planning awaiting a manual /dag call. See commit message.)"""
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a tool", db))
        assert result["status"] == "awaiting_confirmation"


# ===========================================================================
# Model Overrides
# ===========================================================================

@pytest.mark.smoke
class TestRefineIdeaModelOverrides:
    """refine_idea() passes model_overrides through to get_model."""

    def test_model_overrides_used(self):
        """Sprint E.7: model_overrides flows through as ``overrides=`` to
        model_router.generate alongside ``role="model_general"``. The actual
        model lookup happens inside model_router (covered there)."""
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            _run(refine_idea(
                "Build a tool", db,
                model_overrides={"model_general": "custom-model:7b"},
            ))
        call_kwargs = mock_mr.tool_call.call_args.kwargs
        assert call_kwargs.get("role") == "model_general"
        assert call_kwargs.get("overrides") == {"model_general": "custom-model:7b"}
        assert "model" not in call_kwargs


# ===========================================================================
# DB Interactions
# ===========================================================================

@pytest.mark.smoke
class TestRefineIdeaDBInteractions:
    """refine_idea() creates a job and commits properly."""

    def test_creates_job_record(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.tool_call = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            _run(refine_idea("Build a CLI tool", db))
        # Should have called execute (single INSERT refining + final UPDATE) and commit
        assert db.execute.call_count >= 2  # INSERT refining + final UPDATE
        assert db.commit.call_count >= 2   # after INSERT + after final UPDATE


class TestDomainEnumGuard:
    """§17.515 — the refinement LLM must NOT auto-select eng_design (the
    circuits/EDA partition, explicit-override-only). It was leaking on software
    tasks via the 'design' keyword (e.g. "blue-green DEPLOYMENT" → eng_design),
    then getting empty/wrong RAG grounding at execution. It stays in
    ALLOWED_DOMAINS so an explicit /ideate override is still accepted."""

    def test_llm_enum_excludes_eng_design(self):
        from app.modules.idea_refinement import REFINE_BRIEF_TOOL, ALLOWED_DOMAINS
        enum = REFINE_BRIEF_TOOL.input_schema["properties"]["domain"]["enum"]
        assert "eng_design" not in enum, "LLM must not auto-pick eng_design"
        assert {"eng", "llm", "rag", "prompt", "spec"} <= set(enum)
        assert "eng_design" in ALLOWED_DOMAINS  # explicit override still allowed

    def test_domain_field_has_semantic_guidance(self):
        from app.modules.idea_refinement import REFINE_BRIEF_TOOL
        desc = (REFINE_BRIEF_TOOL.input_schema["properties"]["domain"]
                .get("description", "")).lower()
        assert "software" in desc and "eng" in desc  # tells the model what eng is
