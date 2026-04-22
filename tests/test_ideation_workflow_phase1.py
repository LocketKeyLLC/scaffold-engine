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
    _mod.get_model = MagicMock(return_value="qwen3:4b")

    db = AsyncMock()
    await _mod.analyze_and_confirm(idea_text="x", db=db)

    # get_model should have been asked for 'model_router', never 'model_general'
    called_roles = [c.args[0] for c in _mod.get_model.call_args_list]
    assert "model_router" in called_roles, f"Expected 'model_router' in {called_roles}"
    assert "model_general" not in called_roles, (
        f"#6.1 regression: 'model_general' should not be used for distillation. "
        f"Got roles: {called_roles}"
    )
