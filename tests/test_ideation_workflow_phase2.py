"""Tests for ideation_workflow — Phase 2 (research_and_compile) — atomic claim, research, distill, ingest, compile.

Split from the original test_ideation_workflow.py (#9.6).
Shared imports + module-loader live in _ideation_workflow_shared.
"""
from tests._ideation_workflow_shared import *  # noqa: F401, F403

@pytest.mark.smoke
async def test_research_happy_path():
    """Atomic claim succeeds -> research -> ingest -> planning."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["RAG"]},
            "brief": {"title": "RAG Chatbot", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod.search_searxng = AsyncMock(return_value=[
        {"title": "RAG Guide", "url": "https://e.com/a", "content": "chunk..."},
    ])
    distilled = [{"content": "RAG benefits from chunking"}]
    workflow = {
        "compiled_prompt": "Build RAG",
        "workflow_steps": [],
        "configuration": {"domain": "eng", "estimated_nodes": 3},
    }
    _mod.model_router = MagicMock()
    # §17.x — distill now flows through the shared distill_entries tool-call
    # primitive; compile remains the only model_router.generate call.
    _mod.model_router.generate = AsyncMock(
        return_value=_llm_response(json.dumps(workflow))
    )
    _mod.distill_entries = AsyncMock(return_value=distilled)
    _mod.parse_json_object = MagicMock(return_value=workflow)
    _mod.ingest_entries = AsyncMock(return_value={"new": 1, "versioned": 0})
    _mod.format_toon_rows = MagicMock(return_value=["row1"])

    result = await _mod.research_and_compile(job_id="job-001", db=db)

    assert result["status"] == "planning"
    assert result["research_summary"]["facts_extracted"] == 1
    assert result["research_summary"]["milvus_ingested"] == 1


@pytest.mark.smoke
async def test_research_job_not_found():
    """Claim fails + disambiguation SELECT finds nothing -> 404."""
    db = _mock_db_for_claim(claimed_row=None, existing_row_after_fail=None)

    result = await _mod.research_and_compile(job_id="nonexistent", db=db)

    assert result["status"] == "failed"
    assert result["http_status"] == 404
    assert "not found" in result["error"].lower()


@pytest.mark.smoke
async def test_research_wrong_status_returns_409_conflict():
    """Claim fails + job exists in wrong status -> 409 conflict (not failed)."""
    db = _mock_db_for_claim(
        claimed_row=None,
        existing_row_after_fail={"status": "researching"},  # e.g. another caller claimed it
    )

    result = await _mod.research_and_compile(job_id="job-race", db=db)

    assert result["status"] == "conflict", (
        "Concurrent/wrong-status calls must return 'conflict', not 'failed', "
        "so main.py can map to HTTP 409."
    )
    assert result["http_status"] == 409
    assert "researching" in result["error"]


@pytest.mark.smoke
async def test_research_uses_model_router_not_general():
    """#6.1: distillation + compile LLM calls must use 'model_router', not 'model_general'."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["Python RAG"]},
            "brief": {"title": "Test", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod.search_searxng = AsyncMock(return_value=[
        {"title": "T", "url": "https://e.com/x", "content": "snip"},
    ])
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(return_value=_llm_response(
        json.dumps({"compiled_prompt": "x", "workflow_steps": [],
                    "configuration": {"domain": "eng"}})
    ))
    _mod.distill_entries = AsyncMock(return_value=[{"content": "fact"}])
    _mod.parse_json_object = MagicMock(return_value={
        "compiled_prompt": "x", "workflow_steps": [],
        "configuration": {"domain": "eng"},
    })
    _mod.ingest_entries = AsyncMock(return_value={"new": 1, "versioned": 0})
    _mod.format_toon_rows = MagicMock(return_value=["r"])
    from app.config import settings as _real_settings
    _mod.settings.ideation_model_role = _real_settings.ideation_model_role

    await _mod.research_and_compile(job_id="job-mr", db=db)

    # Sprint E.7 / §17.x: both LLM calls must use the configured ideation role.
    # Distill now routes via distill_entries(route=...) (native tool-call);
    # compile still goes through model_router.generate(role=...).
    from app.config import settings
    distill_route = _mod.distill_entries.call_args.kwargs.get("route", {})
    assert distill_route.get("role") == settings.ideation_model_role, (
        f"Distill must route through '{settings.ideation_model_role}'. "
        f"Got route: {distill_route}"
    )
    compile_roles = [
        c.kwargs.get("role")
        for c in _mod.model_router.generate.call_args_list
    ]
    assert settings.ideation_model_role in compile_roles, (
        f"Compile must use '{settings.ideation_model_role}'. Got: {compile_roles}"
    )


@pytest.mark.smoke
async def test_research_user_feedback_folded_into_brief():
    """User feedback passed via /confirm must reach the compile LLM prompt."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["Q"]},
            "brief": {"title": "T", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod.search_searxng = AsyncMock(return_value=[
        {"title": "T", "url": "https://e.com/y", "content": "snip"},
    ])
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(return_value=_llm_response(
        json.dumps({"compiled_prompt": "x", "workflow_steps": [],
                    "configuration": {"domain": "eng"}})
    ))
    _mod.distill_entries = AsyncMock(return_value=[{"content": "f"}])
    _mod.parse_json_object = MagicMock(return_value={
        "compiled_prompt": "x", "workflow_steps": [],
        "configuration": {"domain": "eng"},
    })
    _mod.ingest_entries = AsyncMock(return_value={"new": 1, "versioned": 0})
    _mod.format_toon_rows = MagicMock(return_value=["r"])

    feedback = "Focus on Python, not Java."
    await _mod.research_and_compile(job_id="job-fb", db=db, user_feedback=feedback)

    # §17.x — distill is now distill_entries; compile is the only generate call.
    compile_call = _mod.model_router.generate.call_args_list[0]
    compile_prompt = compile_call.args[0] if compile_call.args else ""
    assert feedback in compile_prompt


@pytest.mark.smoke
async def test_research_empty_search_results_still_reaches_planning():
    """Zero SearXNG results -> skip distill, still compile + reach planning."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["niche"]},
            "brief": {"title": "Niche", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod.search_searxng = AsyncMock(return_value=[])  # no results
    _mod.model_router = MagicMock()
    # Only ONE LLM call expected (compile); distillation is skipped
    _mod.model_router.generate = AsyncMock(return_value=_llm_response(
        json.dumps({"compiled_prompt": "x", "workflow_steps": [],
                    "configuration": {"domain": "eng"}})
    ))
    _mod.parse_json_object = MagicMock(return_value={
        "compiled_prompt": "x", "workflow_steps": [],
        "configuration": {"domain": "eng"},
    })
    _mod.ingest_entries = AsyncMock(return_value={"new": 0, "versioned": 0})
    _mod.format_toon_rows = MagicMock(return_value=[])

    result = await _mod.research_and_compile(job_id="job-empty", db=db)

    assert result["status"] == "planning"
    assert result["research_summary"]["results_found"] == 0
    assert result["research_summary"]["facts_extracted"] == 0
    # Verify distillation was skipped (exactly one LLM call = compile only)
    assert _mod.model_router.generate.call_count == 1
