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
    # §17.580 — feasibility now goes through model_router.tool_call +
    # read_tool_args (the real dep is loaded), not generate + parse_json_object.
    _mod.model_router = MagicMock()
    _mod.model_router.tool_call = AsyncMock(
        return_value=_tool_response(feasibility)
    )

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
    # §17.580 — genuine failure: tool_call unsuccessful / no tool_calls →
    # read_tool_args returns None → graceful fallback dict.
    _mod.model_router = MagicMock()
    _mod.model_router.tool_call = AsyncMock(
        return_value=_tool_response(None, success=False)
    )

    db = AsyncMock()
    result = await _mod.analyze_and_confirm(idea_text="build something", db=db)

    assert result["status"] == "awaiting_confirmation"
    assert result["feasibility"]["feasible"] is True
    assert result["feasibility"]["confidence"] == 0.5
    assert result["feasibility"]["fallback"] is True


@pytest.mark.smoke
async def test_analyze_uses_model_router_not_general():
    """#6.1: feasibility call must resolve role 'model_router', not 'model_general'."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation",
        "job_id": "job-003",
        "refined_brief": {"title": "X", "domain": "eng"},
    })
    _mod.model_router = MagicMock()
    _mod.model_router.tool_call = AsyncMock(
        return_value=_tool_response({"feasible": True, "confidence": 0.7, "summary": "ok"})
    )
    # Pin the mocked settings to the real configured role so production code
    # and assertion both reference the same string.
    from app.config import settings as _real_settings
    _mod.settings.ideation_model_role = _real_settings.ideation_model_role

    db = AsyncMock()
    await _mod.analyze_and_confirm(idea_text="x", db=db)

    # §17.580: feasibility routes through model_router.tool_call (was generate).
    # The configured ideation role (default "model_general") must be passed.
    # Audit #6.1 originally mandated "model_router"; April 26 2026 made it
    # configurable via IDEATION_MODEL_ROLE since model_general now resolves
    # to a cloud model, not a local one.
    from app.config import settings
    called_roles = [
        c.kwargs.get("role")
        for c in _mod.model_router.tool_call.call_args_list
    ]
    assert settings.ideation_model_role in called_roles, (
        f"Expected configured role '{settings.ideation_model_role}' in {called_roles}"
    )


@pytest.mark.smoke
async def test_feasibility_tool_call_immune_to_reasoning_prose():
    """§17.580 regression: the feasibility pass reads structured tool-call args,
    so a reasoning model that emits <think> prose in .text no longer forces the
    fallback (the pre-fix generate() + parse_json_object bug that fired on
    every job because model_general → a reasoning model emits prose)."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation",
        "job_id": "job-580",
        "refined_brief": {"title": "Intake parser", "domain": "eng"},
    })
    feasibility = {"feasible": True, "confidence": 0.9, "summary": "Achievable."}
    # .text is pure reasoning prose (what broke the old path); the real args
    # ride on .tool_calls[0].arguments, which read_tool_args reads instead.
    resp = _tool_response(feasibility)
    resp.text = "<think>Consider the CPU-only constraints and stdlib scope…</think>"
    _mod.model_router = MagicMock()
    _mod.model_router.tool_call = AsyncMock(return_value=resp)

    db = AsyncMock()
    result = await _mod.analyze_and_confirm(idea_text="build an intake parser", db=db)

    # tool_call was used, with the feasibility tool.
    assert _mod.model_router.tool_call.called
    tools_arg = _mod.model_router.tool_call.call_args.kwargs["tools"]
    assert tools_arg[0].name == "emit_feasibility_assessment"
    # No fallback: the real parsed values survived, immune to the .text prose.
    assert result["feasibility"]["confidence"] == 0.9
    assert result["feasibility"].get("fallback") is not True
    assert "⚠️" not in result["message"]


@pytest.mark.smoke
async def test_feasibility_empty_args_dict_falls_back():
    """§17.582 — an empty-args {} (native providers coerce missing args to {},
    not None) must still trigger the fallback: the guard is now `not feasibility`,
    not the weaker `feasibility is None`."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation", "job_id": "job-ea",
        "refined_brief": {"title": "X", "domain": "eng"},
    })
    _mod.model_router = MagicMock()
    _mod.model_router.tool_call = AsyncMock(return_value=_tool_response({}))

    db = AsyncMock()
    result = await _mod.analyze_and_confirm(idea_text="x", db=db)

    assert result["feasibility"]["fallback"] is True
    assert result["feasibility"]["confidence"] == 0.5
    # §17.583 — the retry-on-empty-args re-draw now lives inside
    # model_router.tool_call (tested in test_model_router_tool_call.py); the
    # ideation layer makes a single call, so there's no redraw test here.
