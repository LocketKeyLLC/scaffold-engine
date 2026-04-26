"""
tests/test_idea_refinement.py - Behavioral tests for idea refinement module

Tests refine_idea() by mocking model_router.generate and the DB session,
then verifying output structure, error handling, and domain override.

Run:  docker exec scaffold-orchestrator pytest tests/test_idea_refinement.py -m smoke --timeout=30 -v
"""

import json
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


def _make_llm_response(success=True, text_body=None, error=None):
    """Build a mock LLM response object matching model_router.generate return."""
    resp = MagicMock()
    resp.success = success
    resp.error = error
    resp.model = "qwen3:4b"
    resp.total_duration_ms = 1234
    if text_body is None and success:
        text_body = json.dumps({
            "title": "Test Project",
            "description": "A test project description",
            "goals": ["Goal 1", "Goal 2"],
            "constraints": ["Constraint 1"],
            "domain": "eng",
        })
    resp.text = text_body or ""
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
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert isinstance(result, dict)
        assert "job_id" in result
        assert result["job_id"] == "job-abc-123"

    def test_returns_status_planning(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert result["status"] == "planning"

    def test_returns_refined_brief(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert "refined_brief" in result
        assert isinstance(result["refined_brief"], dict)
        assert result["refined_brief"]["title"] == "Test Project"

    def test_returns_model_used_and_duration(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a CLI tool", db))
        assert result["model_used"] == "qwen3:4b"
        assert result["duration_ms"] == 1234

    def test_calls_generate_with_idea_text(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            _run(refine_idea("Build a weather app", db))
        call_args = mock_mr.generate.call_args
        # The idea text should appear in the prompt (first positional arg)
        assert "Build a weather app" in call_args[0][0]


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
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build something", db))
        assert result["status"] == "failed"
        assert "error" in result

    def test_unparseable_json_returns_failed(self):
        db = _make_db()
        resp = _make_llm_response(text_body="This is not JSON at all")
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=resp)
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
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a tool", db, domain="rag"))
        assert result["refined_brief"]["domain"] == "rag"

    def test_no_domain_keeps_llm_value(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=resp)
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
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea(
                "Build a tool", db, target_status="awaiting_confirmation"
            ))
        assert result["status"] == "awaiting_confirmation", (
            "target_status parameter must drive the returned status, not be ignored."
        )

    def test_default_target_status_is_planning(self):
        """When target_status is not supplied, default 'planning' must be returned."""
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr:
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            result = _run(refine_idea("Build a tool", db))
        assert result["status"] == "planning"


# ===========================================================================
# Model Overrides
# ===========================================================================

@pytest.mark.smoke
class TestRefineIdeaModelOverrides:
    """refine_idea() passes model_overrides through to get_model."""

    def test_model_overrides_used(self):
        db = _make_db()
        resp = _make_llm_response()
        with patch("app.modules.idea_refinement.model_router") as mock_mr, \
             patch("app.modules.idea_refinement.get_model",
                   return_value="custom-model:7b") as mock_gm:
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            _run(refine_idea(
                "Build a tool", db,
                model_overrides={"model_general": "custom-model:7b"},
            ))
        # get_model should have been called with the overrides dict
        mock_gm.assert_called_once_with(
            "model_general",
            {"model_general": "custom-model:7b"},
        )


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
            mock_mr.generate = AsyncMock(return_value=resp)
            from app.modules.idea_refinement import refine_idea
            _run(refine_idea("Build a CLI tool", db))
        # Should have called execute (single INSERT refining + final UPDATE) and commit
        assert db.execute.call_count >= 2  # INSERT refining + final UPDATE
        assert db.commit.call_count >= 2   # after INSERT + after final UPDATE
