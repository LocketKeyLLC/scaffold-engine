"""Tests for ideation_workflow — Phase 1 (analyze_and_confirm) — refine + feasibility, halt at awaiting_confirmation.

Split from the original test_ideation_workflow.py (#9.6).
Shared imports + module-loader live in _ideation_workflow_shared.
"""
from tests._ideation_workflow_shared import *  # noqa: F401, F403

@pytest.mark.smoke
async def test_analyze_happy_path():
    """Phase 1 completes with status=awaiting_confirmation + feasibility dict."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation",
        "job_id": "job-001",
        "refined_brief": {"title": "RAG Chatbot", "domain": "eng"},
    })
    feasibility = {
        "feasible": True, "confidence": 0.85,
        "risks": [], "clarifications_needed": [],
        "recommended_research_queries": ["RAG best practices"],
        "summary": "Feasible.",
    }
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(
        return_value=_llm_response(json.dumps(feasibility))
    )
    _mod.parse_json_object = MagicMock(return_value=feasibility)

    db = AsyncMock()
    result = await _mod.analyze_and_confirm(idea_text="Build a RAG chatbot", db=db)

    assert result["status"] == "awaiting_confirmation"
    assert result["feasibility"]["feasible"] is True
    assert result["feasibility"]["confidence"] == 0.85
    assert db.commit.called


@pytest.mark.smoke
async def test_analyze_refinement_fails():
    """When refine_idea returns failed, Phase 1 short-circuits."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "failed", "error": "LLM garbage",
    })
    db = AsyncMock()
    result = await _mod.analyze_and_confirm(idea_text="nonsense", db=db)

    assert result["status"] == "failed"
    assert not db.execute.called


@pytest.mark.smoke
async def test_analyze_feasibility_llm_fails():
    """Feasibility LLM failure -> graceful fallback dict, still halts at confirmation."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation",
        "job_id": "job-002",
        "refined_brief": {"title": "Test", "domain": "eng"},
    })
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(return_value=_llm_response("", success=False))
    _mod.parse_json_object = MagicMock(return_value=None)

    db = AsyncMock()
    result = await _mod.analyze_and_confirm(idea_text="build something", db=db)

    assert result["status"] == "awaiting_confirmation"
    assert result["feasibility"]["feasible"] is True
    assert result["feasibility"]["confidence"] == 0.5


@pytest.mark.smoke
async def test_analyze_uses_model_router_not_general():
    """#6.1: feasibility call must resolve role 'model_router', not 'model_general'."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation",
        "job_id": "job-003",
        "refined_brief": {"title": "X", "domain": "eng"},
    })
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(
        return_value=_llm_response(json.dumps({"feasible": True}))
    )
    _mod.parse_json_object = MagicMock(return_value={"feasible": True})
    # Pin the mocked settings to the real configured role so production code
    # and assertion both reference the same string.
    from app.config import settings as _real_settings
    _mod.settings.ideation_model_role = _real_settings.ideation_model_role

    db = AsyncMock()
    await _mod.analyze_and_confirm(idea_text="x", db=db)

    # Sprint E.7: model_router.generate now receives role= directly. The
    # configured ideation role (default "model_general") must be the one passed.
    # Audit #6.1 originally mandated "model_router"; April 26 2026 made it
    # configurable via IDEATION_MODEL_ROLE since model_general now resolves
    # to a cloud model, not a local one.
    from app.config import settings
    called_roles = [
        c.kwargs.get("role")
        for c in _mod.model_router.generate.call_args_list
    ]
    assert settings.ideation_model_role in called_roles, (
        f"Expected configured role '{settings.ideation_model_role}' in {called_roles}"
    )
