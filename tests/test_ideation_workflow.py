"""
tests/test_ideation_workflow.py — Smoke tests for the ideation pipeline.

What this file tests:
  • analyze_and_confirm  (Phase 1) — takes a raw idea, structures it,
    checks feasibility with an LLM, then pauses the job for your review.
  • research_and_compile (Phase 2) — takes a confirmed job, searches the
    web, distills facts, stores knowledge, and builds a workflow plan.

Every external service (LLM, search engine, database, Milvus) is replaced
with a controllable fake so tests run in seconds with zero network access.

Uses the importlib loader pattern from test_dag_generator.py so the tests
work inside Docker without import collisions.

Run just these tests:
    python -m pytest tests/test_ideation_workflow.py -v -m smoke
"""

import importlib.util
import json
import os
import sys
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# importlib loader (same Docker-safe pattern as test_dag_generator.py)
# ---------------------------------------------------------------------------
# Why this instead of `from app.modules import ideation_workflow`?
# Inside the Docker container, the working directory is /app, which collides
# with the `app` Python package.  Loading by file path sidesteps that.

_MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "app", "modules", "ideation_workflow.py")
)


def _load_module():
    """Load ideation_workflow.py via importlib, stubbing heavy deps."""
    stubs = {}
    for mod_name in [
        "app", "app.config", "app.model_router",
        "app.modules", "app.modules.idea_refinement",
        "app.modules.gt_extractor", "app.modules.rag_pipeline",
        "app.database",
        "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
        "sqlalchemy.orm", "sqlalchemy.sql",
        "structlog", "aiohttp", "asyncpg",
    ]:
        stubs[mod_name] = MagicMock()

    # sqlalchemy.text must be a callable that returns the SQL string unchanged
    # (the real code does: from sqlalchemy import text; db.execute(text("...")))
    stubs["sqlalchemy"].text = lambda s: s

    # app.config.settings needs a model_general attribute
    stubs["app.config"].settings.model_general = "qwen2.5:7b"

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            "ideation_workflow", _MODULE_PATH
        )
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass
        return mod


_mod = None
try:
    _mod = _load_module()
except Exception:
    pass

pytestmark = pytest.mark.skipif(
    _mod is None or not hasattr(_mod, "analyze_and_confirm"),
    reason="ideation_workflow.py not loadable in this environment",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine in a fresh event loop (existing project pattern)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_mock_db_for_phase2(job_row):
    """
    Build a mock AsyncSession for research_and_compile.

    The real function calls db.execute() then row.mappings().first().
    conftest's make_mock_db wires .mappings().all() — we also need .first().
    """
    mappings_obj = MagicMock()
    mappings_obj.first.return_value = job_row
    mappings_obj.all.return_value = [job_row] if job_row else []

    result_obj = MagicMock()
    result_obj.mappings.return_value = mappings_obj

    db = AsyncMock()
    db.execute.return_value = result_obj
    return db


def _llm_response(text_content: str, success: bool = True):
    """
    Build a fake LLM response object.

    The real model_router.generate() returns an object with .success and .text.
    This creates a lightweight stand-in with those two attributes.
    """
    resp = MagicMock()
    resp.success = success
    resp.text = text_content
    return resp


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 1 — analyze_and_confirm
# ═══════════════════════════════════════════════════════════════════════════
#
# What does this function do?
#   1. Takes your raw idea (e.g. "Build a RAG chatbot")
#   2. Calls refine_idea() to add structure (title, summary, domain)
#   3. Asks the LLM: "Is this idea feasible?"
#   4. Saves the job with status "awaiting_confirmation" — a pause point
#      so YOU can review before expensive research begins.
#
# Why test it?
#   This is the front door of the entire pipeline.  If it breaks silently,
#   ideas vanish or the API server crashes — and nothing warns you.


@pytest.mark.smoke
def test_analyze_happy_path():
    """
    TEST 1 — Happy path: everything works perfectly.
    ─────────────────────────────────────────────────
    You submit a good idea.  refine_idea structures it.  The LLM says
    "yes, feasible."  The job lands at "awaiting_confirmation" so you
    can review it before research begins.

    What we verify:
      ✓ Status is "awaiting_confirmation"
      ✓ Feasibility says feasible=True with a confidence score
      ✓ The refined brief is included in the result
    """
    # -- Fake: refine_idea succeeds --
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation",
        "job_id": "job-001",
        "refined_brief": {
            "title": "RAG Chatbot",
            "description": "A retrieval-augmented chatbot",
            "domain": "eng",
        },
    })

    # -- Fake: LLM returns feasibility JSON --
    feasibility_json = json.dumps({
        "feasible": True,
        "confidence": 0.85,
        "risks": ["Embedding model size"],
        "clarifications_needed": [],
        "recommended_research_queries": ["RAG best practices"],
        "summary": "Feasible with current stack.",
    })
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(return_value=_llm_response(feasibility_json))

    db = AsyncMock()

    result = _run(_mod.analyze_and_confirm(
        idea_text="Build a RAG chatbot for documentation",
        db=db,
        model="qwen2.5:7b",
        domain="eng",
    ))

    assert result["status"] == "awaiting_confirmation", (
        "Job should pause at 'awaiting_confirmation' for your review."
    )
    assert result["feasibility"]["feasible"] is True
    assert result["feasibility"]["confidence"] == 0.85
    assert result["refined_brief"]["title"] == "RAG Chatbot"
    assert db.commit.called, "Changes should be saved to the database."


@pytest.mark.smoke
def test_analyze_refinement_fails():
    """
    TEST 2 — refine_idea fails (e.g. LLM returned garbage).
    ────────────────────────────────────────────────────────
    If the idea-structuring step fails, the function should return that
    failure cleanly — NOT crash or leave the database in a broken state.

    Think of it like a mail sorting machine: if it can't read the address,
    it puts the letter in the "return to sender" bin — it doesn't jam.
    """
    _mod.refine_idea = AsyncMock(return_value={
        "status": "failed",
        "error": "LLM returned unparseable output",
    })

    db = AsyncMock()

    result = _run(_mod.analyze_and_confirm(
        idea_text="vague nonsense",
        db=db,
        model="qwen2.5:7b",
    ))

    assert result["status"] == "failed", (
        "When refinement fails, the result should say 'failed'."
    )
    # The function should bail out early — no DB update, no LLM call
    assert not db.execute.called, (
        "Should not try to update the database when refinement failed."
    )


@pytest.mark.smoke
def test_analyze_feasibility_llm_fails():
    """
    TEST 3 — Feasibility LLM fails (timeout, bad JSON, etc.).
    ──────────────────────────────────────────────────────────
    Refinement works, but the LLM can't judge feasibility.  The function
    should use a safe default: feasible=True, confidence=0.5.  Translation:
    "I couldn't check, so let's assume it might work and let the human decide."

    This is called "graceful degradation" — like a car's GPS losing signal
    but still showing the last known route instead of a blank screen.
    """
    _mod.refine_idea = AsyncMock(return_value={
        "status": "awaiting_confirmation",
        "job_id": "job-002",
        "refined_brief": {"title": "Test Idea", "domain": "eng"},
    })

    # LLM returns success=False (simulating a failed generation)
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(
        return_value=_llm_response("", success=False)
    )

    db = AsyncMock()

    result = _run(_mod.analyze_and_confirm(
        idea_text="Build something cool",
        db=db,
    ))

    assert result["status"] == "awaiting_confirmation", (
        "Even when the LLM fails, the job should still reach 'awaiting_confirmation'."
    )
    assert result["feasibility"]["feasible"] is True, (
        "Safe default: assume feasible when we can't check."
    )
    assert result["feasibility"]["confidence"] == 0.5, (
        "Safe default: 50% confidence means 'uncertain'."
    )


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 2 — research_and_compile
# ═══════════════════════════════════════════════════════════════════════════
#
# What does this function do?
#   1. Loads the confirmed job from the database
#   2. Searches the web via SearXNG for relevant info
#   3. Asks the LLM to distill search results into useful facts
#   4. Stores those facts in Milvus (the knowledge base)
#   5. Compiles a prompt + workflow plan
#   6. Moves the job to "planning" status
#
# Think of it as: your idea was approved, now this function does all
# the homework (research) and hands you a plan.


@pytest.mark.smoke
def test_research_happy_path():
    """
    TEST 4 — Happy path: research completes successfully.
    ─────────────────────────────────────────────────────
    The job exists and is confirmed.  Web search finds results.  The LLM
    distills them.  Knowledge goes into Milvus.  Job moves to "planning."

    What we verify:
      ✓ Final status is "planning"
      ✓ Research summary has fact counts
      ✓ ingest_entries was called (knowledge was stored)
      ✓ Database was updated
    """
    # -- Fake: database returns a confirmed job --
    job_row = {
        "status": "awaiting_confirmation",
        "research_data": {
            "feasibility": {
                "recommended_research_queries": ["RAG best practices"],
            },
            "brief": {"title": "RAG Chatbot", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _make_mock_db_for_phase2(job_row)

    # -- Fake: SearXNG returns search results --
    _mod._search_searxng = AsyncMock(return_value=[
        {"title": "RAG Guide", "url": "https://example.com/rag", "content": "Use chunking..."},
        {"title": "Milvus Intro", "url": "https://example.com/milvus", "content": "Vector DB..."},
    ])

    # -- Fake: LLM distills facts, then compiles workflow --
    distilled_facts = json.dumps([
        {"content": "RAG systems benefit from chunking"},
        {"content": "Milvus handles vector similarity"},
    ])
    compiled_workflow = json.dumps({
        "compiled_prompt": "Build a RAG chatbot using chunking + Milvus",
        "workflow_steps": [{"step": 1, "action": "Set up Milvus", "tool": "Milvus", "notes": ""}],
        "configuration": {"domain": "eng", "estimated_nodes": 3},
    })
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(
        side_effect=[
            _llm_response(distilled_facts),   # first call: distillation
            _llm_response(compiled_workflow),  # second call: compilation
        ]
    )

    # -- Fake: Milvus ingest succeeds --
    _mod.ingest_entries = AsyncMock(return_value=2)

    # -- Fake: TOON formatting --
    _mod._format_toon_rows = MagicMock(return_value=["row1", "row2"])

    result = _run(_mod.research_and_compile(
        job_id="job-001",
        db=db,
        model="qwen2.5:7b",
        push_to_github=False,
    ))

    assert result["status"] == "planning", (
        "After successful research, the job should move to 'planning'."
    )
    assert result["research_summary"]["facts_extracted"] == 2
    assert result["research_summary"]["milvus_ingested"] == 2
    assert db.commit.called


@pytest.mark.smoke
def test_research_job_not_found():
    """
    TEST 5 — Job doesn't exist.
    ───────────────────────────
    Someone passes a job ID that isn't in the database — maybe a typo.
    The function should return a clear error, NOT crash.
    """
    db = _make_mock_db_for_phase2(None)  # .first() returns None

    result = _run(_mod.research_and_compile(
        job_id="nonexistent-id",
        db=db,
    ))

    assert result["status"] == "failed"
    assert "not found" in result["error"].lower(), (
        "Error message should tell the user the job wasn't found."
    )


@pytest.mark.smoke
def test_research_wrong_status():
    """
    TEST 6 — Job exists but hasn't been confirmed yet.
    ──────────────────────────────────────────────────
    Like trying to build a house before the blueprints are approved.
    The function should refuse and explain the problem.
    """
    job_row = {
        "status": "refining",  # wrong — should be "awaiting_confirmation"
        "research_data": None,
        "refined_brief": None,
    }
    db = _make_mock_db_for_phase2(job_row)

    result = _run(_mod.research_and_compile(
        job_id="job-wrong-status",
        db=db,
    ))

    assert result["status"] == "failed"
    assert "awaiting_confirmation" in result["error"], (
        "Error should explain what status was expected."
    )


@pytest.mark.smoke
def test_research_user_feedback_included():
    """
    TEST 7 — User gave feedback during confirmation.
    ────────────────────────────────────────────────
    When you reviewed the idea, you typed "Focus on Python, not Java."
    That feedback should be folded into the brief so the research
    respects your preference.

    What we verify:
      ✓ The function completes successfully
      ✓ The feedback text ends up in the compile prompt sent to the LLM
    """
    job_row = {
        "status": "awaiting_confirmation",
        "research_data": {
            "feasibility": {"recommended_research_queries": ["Python RAG"]},
            "brief": {"title": "RAG Chatbot", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _make_mock_db_for_phase2(job_row)

    _mod._search_searxng = AsyncMock(return_value=[
        {"title": "Python RAG", "url": "https://example.com/py", "content": "LangChain..."},
    ])
    _mod.model_router = MagicMock()
    _mod.model_router.generate = AsyncMock(
        side_effect=[
            _llm_response(json.dumps([{"content": "Python RAG fact"}])),
            _llm_response(json.dumps({
                "compiled_prompt": "Python-focused RAG",
                "workflow_steps": [],
                "configuration": {"domain": "eng", "estimated_nodes": 3},
            })),
        ]
    )
    _mod.ingest_entries = AsyncMock(return_value=1)
    _mod._format_toon_rows = MagicMock(return_value=["row1"])

    feedback = "Focus on Python examples, not Java."

    result = _run(_mod.research_and_compile(
        job_id="job-feedback",
        db=db,
        user_feedback=feedback,
        model="qwen2.5:7b",
        push_to_github=False,
    ))

    assert result["status"] == "planning"

    # Verify feedback reached the compile step.
    # The function sets brief["user_feedback"] = user_feedback, then
    # includes the brief in the JSON context sent to the second LLM call.
    compile_call = _mod.model_router.generate.call_args_list[1]
    compile_prompt = compile_call.args[0] if compile_call.args else ""
    assert feedback in compile_prompt, (
        "User feedback should appear in the compile prompt sent to the LLM."
    )


@pytest.mark.smoke
def test_research_empty_search_results():
    """
    TEST 8 — SearXNG returns nothing.
    ─────────────────────────────────
    Maybe it's a niche topic or SearXNG had no matches.  The function
    should NOT crash.  It skips distillation (nothing to distill),
    skips ingest, and still compiles a plan with what it has.

    Like writing a report when the library had no books on the topic —
    you note "no sources found" and proceed with what you know.
    """
    job_row = {
        "status": "awaiting_confirmation",
        "research_data": {
            "feasibility": {"recommended_research_queries": ["obscure niche topic"]},
            "brief": {"title": "Niche Thing", "domain": "eng"},
        },
        "refined_brief": None,
    }
    db = _make_mock_db_for_phase2(job_row)

    _mod._search_searxng = AsyncMock(return_value=[])  # Nothing found

    compiled = json.dumps({
        "compiled_prompt": "Plan with no external research",
        "workflow_steps": [],
        "configuration": {"domain": "eng", "estimated_nodes": 3},
    })
    _mod.model_router = MagicMock()
    # Only ONE LLM call expected — the compile step.
    # Distillation is skipped when there are no search results (the code
    # has: if all_results: ... call LLM).
    _mod.model_router.generate = AsyncMock(return_value=_llm_response(compiled))

    _mod.ingest_entries = AsyncMock(return_value=0)
    _mod._format_toon_rows = MagicMock(return_value=[])

    result = _run(_mod.research_and_compile(
        job_id="job-empty-search",
        db=db,
        push_to_github=False,
    ))

    assert result["status"] == "planning", (
        "Even with no search results, the job should reach 'planning'."
    )
    assert result["research_summary"]["results_found"] == 0
    assert result["research_summary"]["facts_extracted"] == 0
