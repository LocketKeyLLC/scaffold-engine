"""tests/test_ideation_workflow.py — Tests for ideation pipeline (Phase 1 + 2).

Covers the rewritten contract:
  • Phase 1 (analyze_and_confirm) — refine + feasibility, halt at awaiting_confirmation
  • Phase 2 (research_and_compile) — atomic claim, research, distill, ingest, compile
  • Distillation + compile both use model_router (4b), not model_general
  • Atomic claim returns conflict (409) on concurrent calls, not-found (404) on missing

Uses the importlib loader pattern (Docker-safe): stubs heavy deps so the module
loads without a live Postgres/Milvus/Ollama.

Run:
    docker exec scaffold-orchestrator pytest tests/test_ideation_workflow.py -v -m smoke
"""
import asyncio
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "modules", "ideation_workflow.py")
)


def _load_module():
    """Load ideation_workflow.py via importlib with heavy deps stubbed."""
    stubs = {}
    for mod_name in [
        "app", "app.config", "app.model_router",
        "app.modules", "app.modules.idea_refinement",
        "app.modules.gt_extractor", "app.modules.rag_pipeline",
        "app.database",
        "app.utils", "app.utils.llm_parsing", "app.utils.topic_detection",
        "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
        "sqlalchemy.orm", "sqlalchemy.sql",
        "structlog", "aiohttp", "asyncpg",
    ]:
        stubs[mod_name] = MagicMock()

    # sqlalchemy.text passthrough
    stubs["sqlalchemy"].text = lambda s: s

    # settings.ideation_max_* need real ints for slicing
    stubs["app.config"].settings.ideation_max_queries = 5
    stubs["app.config"].settings.ideation_max_distill_results = 15
    stubs["app.config"].settings.model_general = "qwen2.5:7b"

    # structlog .bind() must be chainable and return a logger-like object
    mock_logger = MagicMock()
    mock_logger.bind.return_value = mock_logger
    stubs["structlog"].stdlib.get_logger.return_value = mock_logger

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("ideation_workflow", _MODULE_PATH)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            import traceback
            traceback.print_exc()
        return mod


_mod = _load_module()

pytestmark = pytest.mark.skipif(
    _mod is None or not hasattr(_mod, "analyze_and_confirm"),
    reason="ideation_workflow.py not loadable in this environment",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _llm_response(text_content: str, success: bool = True):
    """Fake model_router.generate() response — object with .success, .text."""
    resp = MagicMock()
    resp.success = success
    resp.text = text_content
    return resp


def _mock_db_for_claim(claimed_row, existing_row_after_fail=None):
    """Build AsyncSession mock for Phase 2's atomic-claim flow.

    The new research_and_compile does:
      1. UPDATE ... RETURNING research_data, refined_brief  -> claim
      2. (if claim empty) SELECT status FROM jobs WHERE id  -> disambiguation
      3. UPDATE jobs SET status='planning', ...             -> final transition

    Args:
        claimed_row: dict the atomic UPDATE RETURNING yields (or None if claim fails)
        existing_row_after_fail: dict with 'status' key for the disambiguation SELECT
            when the claim fails (used to decide between 404 and 409)
    """
    call_results = []

    # Call 1: atomic claim
    claim_mappings = MagicMock()
    claim_mappings.first.return_value = claimed_row
    claim_result = MagicMock()
    claim_result.mappings.return_value = claim_mappings
    call_results.append(claim_result)

    if claimed_row is None:
        # Call 2: disambiguation SELECT
        check_mappings = MagicMock()
        check_mappings.first.return_value = existing_row_after_fail
        check_result = MagicMock()
        check_result.mappings.return_value = check_mappings
        call_results.append(check_result)
    else:
        # Call 2: final UPDATE (doesn't need .mappings but safe to provide)
        final_result = MagicMock()
        call_results.append(final_result)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=call_results)
    return db


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1 — analyze_and_confirm
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.smoke
def test_analyze_happy_path():
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
    result = _run(_mod.analyze_and_confirm(idea_text="Build a RAG chatbot", db=db))

    assert result["status"] == "awaiting_confirmation"
    assert result["feasibility"]["feasible"] is True
    assert result["feasibility"]["confidence"] == 0.85
    assert db.commit.called


@pytest.mark.smoke
def test_analyze_refinement_fails():
    """When refine_idea returns failed, Phase 1 short-circuits."""
    _mod.refine_idea = AsyncMock(return_value={
        "status": "failed", "error": "LLM garbage",
    })
    db = AsyncMock()
    result = _run(_mod.analyze_and_confirm(idea_text="nonsense", db=db))

    assert result["status"] == "failed"
    assert not db.execute.called


@pytest.mark.smoke
def test_analyze_feasibility_llm_fails():
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
    result = _run(_mod.analyze_and_confirm(idea_text="build something", db=db))

    assert result["status"] == "awaiting_confirmation"
    assert result["feasibility"]["feasible"] is True
    assert result["feasibility"]["confidence"] == 0.5


@pytest.mark.smoke
def test_analyze_uses_model_router_not_general():
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
    _run(_mod.analyze_and_confirm(idea_text="x", db=db))

    # get_model should have been asked for 'model_router', never 'model_general'
    called_roles = [c.args[0] for c in _mod.get_model.call_args_list]
    assert "model_router" in called_roles, f"Expected 'model_router' in {called_roles}"
    assert "model_general" not in called_roles, (
        f"#6.1 regression: 'model_general' should not be used for distillation. "
        f"Got roles: {called_roles}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — research_and_compile
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.smoke
def test_research_happy_path():
    """Atomic claim succeeds -> research -> ingest -> planning."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["RAG"]},
            "brief": {"title": "RAG Chatbot", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod._search_searxng = AsyncMock(return_value=[
        {"title": "RAG Guide", "url": "https://e.com/a", "content": "chunk..."},
    ])
    distilled = [{"content": "RAG benefits from chunking"}]
    workflow = {
        "compiled_prompt": "Build RAG",
        "workflow_steps": [],
        "configuration": {"domain": "eng", "estimated_nodes": 3},
    }
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(side_effect=[
        _llm_response(json.dumps(distilled)),
        _llm_response(json.dumps(workflow)),
    ])
    _mod.parse_json_array = MagicMock(return_value=distilled)
    _mod.parse_json_object = MagicMock(return_value=workflow)
    _mod.ingest_entries = AsyncMock(return_value={"new": 1, "versioned": 0})
    _mod._format_toon_rows = MagicMock(return_value=["row1"])

    result = _run(_mod.research_and_compile(job_id="job-001", db=db))

    assert result["status"] == "planning"
    assert result["research_summary"]["facts_extracted"] == 1
    assert result["research_summary"]["milvus_ingested"] == 1


@pytest.mark.smoke
def test_research_job_not_found():
    """Claim fails + disambiguation SELECT finds nothing -> 404."""
    db = _mock_db_for_claim(claimed_row=None, existing_row_after_fail=None)

    result = _run(_mod.research_and_compile(job_id="nonexistent", db=db))

    assert result["status"] == "failed"
    assert result["http_status"] == 404
    assert "not found" in result["error"].lower()


@pytest.mark.smoke
def test_research_wrong_status_returns_409_conflict():
    """Claim fails + job exists in wrong status -> 409 conflict (not failed)."""
    db = _mock_db_for_claim(
        claimed_row=None,
        existing_row_after_fail={"status": "researching"},  # e.g. another caller claimed it
    )

    result = _run(_mod.research_and_compile(job_id="job-race", db=db))

    assert result["status"] == "conflict", (
        "Concurrent/wrong-status calls must return 'conflict', not 'failed', "
        "so main.py can map to HTTP 409."
    )
    assert result["http_status"] == 409
    assert "researching" in result["error"]


@pytest.mark.smoke
def test_research_uses_model_router_not_general():
    """#6.1: distillation + compile LLM calls must use 'model_router', not 'model_general'."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["Python RAG"]},
            "brief": {"title": "Test", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod._search_searxng = AsyncMock(return_value=[
        {"title": "T", "url": "https://e.com/x", "content": "snip"},
    ])
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(side_effect=[
        _llm_response(json.dumps([{"content": "fact"}])),
        _llm_response(json.dumps({"compiled_prompt": "x", "workflow_steps": [],
                                  "configuration": {"domain": "eng"}})),
    ])
    _mod.parse_json_array = MagicMock(return_value=[{"content": "fact"}])
    _mod.parse_json_object = MagicMock(return_value={
        "compiled_prompt": "x", "workflow_steps": [],
        "configuration": {"domain": "eng"},
    })
    _mod.ingest_entries = AsyncMock(return_value={"new": 1, "versioned": 0})
    _mod._format_toon_rows = MagicMock(return_value=["r"])
    _mod.get_model = MagicMock(return_value="qwen3:4b")

    _run(_mod.research_and_compile(job_id="job-mr", db=db))

    called_roles = [c.args[0] for c in _mod.get_model.call_args_list]
    # Phase 2 makes 2 LLM calls (distill + compile) — both must use model_router
    router_calls = called_roles.count("model_router")
    general_calls = called_roles.count("model_general")
    assert router_calls >= 2, (
        f"Expected distill + compile to both use 'model_router' (>=2 calls). "
        f"Got roles: {called_roles}"
    )
    assert general_calls == 0, (
        f"#6.1 regression: 'model_general' must not appear. Got roles: {called_roles}"
    )


@pytest.mark.smoke
def test_research_user_feedback_folded_into_brief():
    """User feedback passed via /confirm must reach the compile LLM prompt."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["Q"]},
            "brief": {"title": "T", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod._search_searxng = AsyncMock(return_value=[
        {"title": "T", "url": "https://e.com/y", "content": "snip"},
    ])
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(side_effect=[
        _llm_response(json.dumps([{"content": "f"}])),
        _llm_response(json.dumps({"compiled_prompt": "x", "workflow_steps": [],
                                  "configuration": {"domain": "eng"}})),
    ])
    _mod.parse_json_array = MagicMock(return_value=[{"content": "f"}])
    _mod.parse_json_object = MagicMock(return_value={
        "compiled_prompt": "x", "workflow_steps": [],
        "configuration": {"domain": "eng"},
    })
    _mod.ingest_entries = AsyncMock(return_value={"new": 1, "versioned": 0})
    _mod._format_toon_rows = MagicMock(return_value=["r"])

    feedback = "Focus on Python, not Java."
    _run(_mod.research_and_compile(job_id="job-fb", db=db, user_feedback=feedback))

    # The second LLM call (compile) should have the feedback in its prompt
    compile_call = _mod.model_router.generate.call_args_list[1]
    compile_prompt = compile_call.args[0] if compile_call.args else ""
    assert feedback in compile_prompt


@pytest.mark.smoke
def test_research_empty_search_results_still_reaches_planning():
    """Zero SearXNG results -> skip distill, still compile + reach planning."""
    claimed = {
        "research_data": {
            "feasibility": {"recommended_research_queries": ["niche"]},
            "brief": {"title": "Niche", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _mock_db_for_claim(claimed)

    _mod._search_searxng = AsyncMock(return_value=[])  # no results
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
    _mod._format_toon_rows = MagicMock(return_value=[])

    result = _run(_mod.research_and_compile(job_id="job-empty", db=db))

    assert result["status"] == "planning"
    assert result["research_summary"]["results_found"] == 0
    assert result["research_summary"]["facts_extracted"] == 0
    # Verify distillation was skipped (exactly one LLM call = compile only)
    assert _mod.model_router.generate.call_count == 1
