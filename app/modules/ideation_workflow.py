"""Scaffold Engine - Ideation-to-Workflow pipeline (Phase 1).

Multi-phase flow between idea_refinement and dag_generator:
  refining -> awaiting_confirmation -> (Phase 2 added later)
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.config import settings
from app.modules.idea_refinement import refine_idea
from app.utils.llm_parsing import parse_json_object, parse_json_array

logger = logging.getLogger("scaffold.ideation")

FEASIBILITY_SYSTEM = (
    "You are a technical feasibility analyst. Given a structured brief, assess:\n"
    "1. Is this achievable with local CPU-only LLM infrastructure (Ollama, Milvus, SearXNG)?\n"
    "2. What are the risks or unknowns?\n"
    "3. What clarifications would improve the plan?\n\n"
    "OUTPUT FORMAT (strict JSON, no markdown fences):\n"
    '{\n'
    '  "feasible": true,\n'
    '  "confidence": 0.8,\n'
    '  "risks": ["risk1"],\n'
    '  "clarifications_needed": ["question1"],\n'
    '  "recommended_research_queries": ["query1", "query2"],\n'
    '  "summary": "one paragraph assessment"\n'
    '}'
)


async def analyze_and_confirm(
    idea_text: str,
    db: AsyncSession,
    model: str | None = None,
    domain: str | None = None,
) -> dict:
    """Phase 1: Refine idea, assess feasibility, halt at awaiting_confirmation."""

    refine_result = await refine_idea(idea_text, db, model=model, domain=domain)

    if refine_result["status"] == "failed":
        return refine_result

    job_id = refine_result["job_id"]
    brief = refine_result["refined_brief"]

    resp = await model_router.generate(
        "Assess this brief:\n" + json.dumps(brief, indent=2),
        model=model or settings.model_general,
        system=FEASIBILITY_SYSTEM,
        temperature=0.2,
        max_tokens=2048,
    )

    feasibility = parse_json_object(resp.text) if resp.success else None
    if feasibility is None:
        feasibility = {
            "feasible": True,
            "confidence": 0.5,
            "risks": ["Could not assess - proceeding with best effort"],
            "clarifications_needed": [],
            "recommended_research_queries": [idea_text],
            "summary": "Feasibility check failed; defaulting to proceed.",
        }

    await db.execute(
        text(
            "UPDATE jobs SET status = 'awaiting_confirmation', "
            "research_data = :data WHERE id = :id"
        ),
        {
            "data": json.dumps({"feasibility": feasibility, "brief": brief}),
            "id": job_id,
        },
    )
    await db.commit()

    return {
        "job_id": job_id,
        "status": "awaiting_confirmation",
        "refined_brief": brief,
        "feasibility": feasibility,
        "message": "Review the analysis. Reply /confirm <job_id> to proceed, or /confirm <job_id> <feedback> to adjust.",
    }


# ---------------------------------------------------------------------------
# Phase 2: Research -> Ingest -> Compile -> Present
# ---------------------------------------------------------------------------

from app.modules.gt_extractor import (
    _search_searxng,
    _format_toon_rows,
    _detect_topic_id,
    _push_to_github,
    TOPIC_MAP,
    DISTILL_SYSTEM,
    DISTILL_PROMPT,
)
from app.modules.rag_pipeline import ingest_entries

COMPILE_SYSTEM = (
    "You are a prompt architect. Given a refined brief and researched facts, produce:\n"
    "1. An optimal prompt for executing this idea via a DAG of LLM nodes\n"
    "2. A step-by-step workflow the user should follow\n\n"
    "OUTPUT FORMAT (strict JSON, no markdown fences):\n"
    '{\n'
    '  "compiled_prompt": "the full prompt text ready for DAG generation",\n'
    '  "workflow_steps": [\n'
    '    {"step": 1, "action": "what to do", "tool": "LLM|CodeGen|SearXNG|Milvus", "notes": "details"}\n'
    '  ],\n'
    '  "configuration": {\n'
    '    "recommended_model": "model tag",\n'
    '    "temperature": 0.3,\n'
    '    "domain": "prompt|rag|llm|spec|eng",\n'
    '    "estimated_nodes": 3\n'
    '  }\n'
    '}'
)


async def research_and_compile(
    job_id: str,
    db: AsyncSession,
    user_feedback: str | None = None,
    model: str | None = None,
    push_to_github: bool = False,
) -> dict:
    """Phase 2: Research via SearXNG, ingest to Milvus, compile prompt, present workflow."""

    # Load job + stashed data
    row = await db.execute(
        text("SELECT status, research_data, refined_brief FROM jobs WHERE id = :id"),
        {"id": job_id},
    )
    job = row.mappings().first()
    if not job:
        return {"status": "failed", "error": f"Job {job_id} not found"}
    if job["status"] != "awaiting_confirmation":
        return {"status": "failed", "error": f"Job is '{job['status']}', expected 'awaiting_confirmation'"}

    stashed = job["research_data"] if job["research_data"] else {}
    brief = stashed.get("brief", {})
    if not brief and job["refined_brief"]:
        brief = job["refined_brief"] if job["refined_brief"] else {}
    feasibility = stashed.get("feasibility", {})

    if user_feedback:
        brief["user_feedback"] = user_feedback

    # Transition -> researching
    await db.execute(
        text("UPDATE jobs SET status = 'researching' WHERE id = :id"),
        {"id": job_id},
    )
    await db.commit()

    # Step 1: SearXNG research
    queries = feasibility.get("recommended_research_queries", [])
    if not queries:
        topic = brief.get("title", "")
        queries = [topic, f"{topic} best practices", f"{topic} implementation"]

    all_results: list[dict] = []
    seen_urls: set[str] = set()
    for q in queries[:5]:
        results = await _search_searxng(q)
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)
        logger.info("ideation_search: %d results for '%s'", len(results), q)

    # Step 2: LLM distillation
    entries: list[dict] = []
    if all_results:
        results_text = "\n\n".join(
            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content']}"
            for r in all_results[:15]
        )
        topic_str = brief.get("title", "unknown")
        resp = await model_router.generate(
            DISTILL_PROMPT.format(topic=topic_str, results=results_text),
            model=model or settings.model_general,
            system=DISTILL_SYSTEM,
            temperature=0.2,
            max_tokens=4096,
        )
        if resp.success:
            entries = parse_json_array(resp.text) or []

    # Step 3: TOON format + Milvus ingest
    toon_rows = _format_toon_rows(entries) if entries else []
    ingest_count = 0
    if entries:
        ingest_count = await ingest_entries(entries, domain=brief.get("domain", "eng"))

    # Optional GitHub push
    gh_result = None
    if push_to_github and toon_rows:
        topic_id = _detect_topic_id(brief.get("title", ""))
        target_file = f"knowledge/{TOPIC_MAP.get(topic_id, 'llm-research')}.toon"
        gh_result = await _push_to_github(toon_rows, target_file, brief.get("title", ""))

    # Step 4: Compile prompt + workflow
    compile_context = json.dumps({
        "brief": brief,
        "researched_facts": [e.get("content", "") for e in entries[:10]],
        "fact_count": len(entries),
    }, indent=2)

    resp = await model_router.generate(
        "Compile an execution plan from this context:\n" + compile_context,
        model=model or settings.model_general,
        system=COMPILE_SYSTEM,
        temperature=0.3,
        max_tokens=4096,
    )

    workflow = parse_json_object(resp.text) if resp.success else None
    if workflow is None:
        workflow = {
            "compiled_prompt": brief.get("description", ""),
            "workflow_steps": [],
            "configuration": {"domain": brief.get("domain", "eng"), "estimated_nodes": 3},
        }

    # Step 5: Persist and transition -> planning
    await db.execute(
        text(
            "UPDATE jobs SET status = 'planning', "
            "research_data = :data, workflow_summary = :workflow "
            "WHERE id = :id"
        ),
        {
            "data": json.dumps({
                "feasibility": feasibility,
                "brief": brief,
                "research_entries": len(entries),
                "milvus_ingested": ingest_count,
            }),
            "workflow": json.dumps(workflow),
            "id": job_id,
        },
    )
    await db.commit()

    return {
        "job_id": job_id,
        "status": "planning",
        "research_summary": {
            "queries_run": len(queries),
            "results_found": len(all_results),
            "facts_extracted": len(entries),
            "milvus_ingested": ingest_count,
            "toon_rows": len(toon_rows),
            "github": gh_result,
        },
        "workflow": workflow,
        "message": "Research complete. Job is now in 'planning' status. DAG generation can proceed via /dag or auto-chain.",
    }


