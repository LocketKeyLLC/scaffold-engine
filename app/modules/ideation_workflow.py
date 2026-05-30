"""Scaffold Engine — Ideation-to-Workflow pipeline.

Multi-phase flow bridging idea refinement and DAG generation:

    pending -> refining -> awaiting_confirmation    (Phase 1)
    awaiting_confirmation -> researching -> planning (Phase 2)

Phase 1 (``analyze_and_confirm``) refines a raw idea and produces a feasibility
assessment, then halts at ``awaiting_confirmation`` pending user review.

Phase 2 (``research_and_compile``) is claimed atomically to prevent double
execution, then runs SearXNG research, LLM distillation, Milvus ingestion, and
prompt compilation before transitioning to ``planning``.
"""
from __future__ import annotations

# stdlib
import json
import asyncio
import logging

# third-party
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session

# local
from app import model_router
from app.config import settings
from app.modules.gt_extractor import (
    DISTILL_PROMPT,
    DISTILL_SYSTEM,
    TOPIC_KEYWORDS,
    TOPIC_MAP,
    format_toon_rows,
    push_to_github as gt_push_to_github,
    search_searxng,
)
from app.modules.idea_refinement import refine_idea
from app.modules.rag_pipeline import ingest_entries
from app.utils.job_utils import fail_job as _fail_job
from app.utils.llm_parsing import parse_json_array, parse_json_object
from app.utils.topic_detection import detect_topic_id

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
    '    "temperature": 0.3,\n'
    '    "domain": "prompt|rag|llm|spec|eng",\n'
    '    "estimated_nodes": 3\n'
    '  }\n'
    '}'
)


async def analyze_and_confirm(
    idea_text: str,
    db: AsyncSession,
    model: str | None = None,
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> dict:
    """Phase 1: refine an idea, assess feasibility, halt at ``awaiting_confirmation``.

    Delegates initial structuring to :func:`app.modules.idea_refinement.refine_idea`
    with ``target_status="awaiting_confirmation"``, then runs a feasibility LLM
    pass (using the router/4b model) and stashes both brief and feasibility on the
    job row.

    Args:
        idea_text: Raw user-submitted idea.
        db: Async SQLAlchemy session.
        model: Optional explicit model tag.
        domain: Optional domain override propagated to refinement.
        model_overrides: Per-request role→model mapping.

    Returns:
        Dict with ``job_id``, ``status``, ``refined_brief``, ``feasibility``, and
        ``message``. On refinement failure, returns the refine_idea failure dict.
    """
    logger.info("phase1_start: idea_preview=%s", idea_text[:80])

    refine_result = await refine_idea(
        idea_text,
        db,
        model=model,
        domain=domain,
        model_overrides=model_overrides,
        target_status="awaiting_confirmation",
    )
    if refine_result["status"] == "failed":
        logger.warning(
            "phase1_refinement_failed: error=%s", refine_result.get("error"),
        )
        return refine_result

    job_id = refine_result["job_id"]
    brief = refine_result["refined_brief"]

    route_kwargs = (
        {"model": model} if model
        else {"role": settings.ideation_model_role, "overrides": model_overrides}
    )
    resp = await model_router.generate(
        "Assess this brief:\n" + json.dumps(brief, indent=2),
        system=FEASIBILITY_SYSTEM,
        temperature=0.2,
        max_tokens=2048,
        **route_kwargs,
    )

    feasibility = parse_json_object(resp.text) if resp.success else None
    feasibility_fallback = feasibility is None
    if feasibility_fallback:
        logger.warning(
            "phase1_feasibility_fallback: job_id=%s llm_success=%s",
            job_id, resp.success,
        )
        feasibility = {
            "feasible": True,
            "confidence": 0.5,
            "risks": ["Could not assess - proceeding with best effort"],
            "clarifications_needed": [],
            "recommended_research_queries": [idea_text],
            "summary": "⚠️ Feasibility check failed; defaulting to proceed.",
            "fallback": True,
        }

    await db.execute(
        text("UPDATE jobs SET research_data = :data WHERE id = :id"),
        {
            "data": json.dumps({"feasibility": feasibility, "brief": brief}),
            "id": job_id,
        },
    )
    await db.commit()

    logger.info(
        "phase1_complete: job_id=%s feasible=%s",
        job_id, feasibility.get("feasible"),
    )
    return {
        "job_id": job_id,
        "status": "awaiting_confirmation",
        "refined_brief": brief,
        "feasibility": feasibility,
        "message": (
            ("⚠️ Feasibility check failed; using best-effort defaults. "
             if feasibility_fallback else "")
            + "Review the analysis. Reply /confirm <job_id> to proceed, "
              "or /confirm <job_id> <feedback> to adjust."
        ),
    }


async def research_and_compile(
    job_id: str,
    db: AsyncSession,
    user_feedback: str | None = None,
    model: str | None = None,
    push_to_github: bool = False,
    model_overrides: dict | None = None,
) -> dict:
    """Phase 2: claim atomically, research, ingest, compile, transition to planning.

    Uses ``UPDATE ... WHERE status='awaiting_confirmation' RETURNING ...`` to
    prevent concurrent ``/confirm`` calls from double-executing. If the claim
    fails (job missing or wrong status), returns a conflict result suitable for
    HTTP 409.

    Args:
        job_id: UUID of a job in ``awaiting_confirmation``.
        db: Async SQLAlchemy session.
        user_feedback: Optional adjustments from ``/confirm <id> <feedback>``.
        model: Optional explicit model tag.
        push_to_github: When True, pushes TOON rows to the configured repo.
        model_overrides: Per-request role→model mapping.

    Returns:
        Success dict with ``status="planning"``, ``research_summary``, and
        ``workflow``. On failure/conflict, returns dict with ``status="failed"``
        or ``status="conflict"`` and an ``http_status`` hint (409 / 404).
    """
    # Atomic claim
    claim = await db.execute(
        text(
            """
            UPDATE jobs
               SET status = 'researching'
             WHERE id = :id
               AND status = 'awaiting_confirmation'
         RETURNING research_data, refined_brief
            """
        ),
        {"id": job_id},
    )
    claimed = claim.mappings().first()
    await db.commit()

    if not claimed:
        check = await db.execute(
            text("SELECT status FROM jobs WHERE id = :id"),
            {"id": job_id},
        )
        existing = check.mappings().first()
        if not existing:
            logger.warning("phase2_job_not_found: job_id=%s", job_id)
            return {
                "status": "failed",
                "error": f"Job {job_id} not found",
                "http_status": 404,
            }
        logger.warning(
            "phase2_claim_conflict: job_id=%s actual_status=%s",
            job_id, existing["status"],
        )
        return {
            "status": "conflict",
            "error": (
                f"Job is '{existing['status']}', not 'awaiting_confirmation' "
                "(may be in progress or already processed)"
            ),
            "http_status": 409,
        }

    stashed = claimed["research_data"] or {}
    brief = stashed.get("brief") or (claimed["refined_brief"] or {})
    feasibility = stashed.get("feasibility", {})

    if user_feedback:
        brief["user_feedback"] = user_feedback

    # FastAPI's get_db owns ``db``'s lifecycle and will close it on
    # request teardown. We do not touch it again here — the long network
    # I/O block below uses fresh short-lived sessions for each write —
    # so there is no need to close the request-scoped session manually.

    try:
        # Step 1: SearXNG research
        queries = feasibility.get("recommended_research_queries", [])
        if not queries:
            topic = brief.get("title", "")
            queries = [topic, f"{topic} best practices", f"{topic} implementation"]

        query_cap = settings.ideation_max_queries
        all_results: list[dict] = []
        seen_urls: set[str] = set()
        for q in queries[:query_cap]:
            results = await search_searxng(q)
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(r)
            logger.info(
                "phase2_search: job_id=%s query=%r result_count=%d",
                job_id, q, len(results),
            )

        # Step 2: LLM distillation (router/4b)
        entries: list[dict] = []
        distill_cap = settings.ideation_max_distill_results
        if all_results:
            results_text = "\n\n".join(
                f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content']}"
                for r in all_results[:distill_cap]
            )
            topic_str = brief.get("title", "unknown")
            distill_route = (
                {"model": model} if model
                else {"role": settings.ideation_model_role, "overrides": model_overrides}
            )
            resp = await model_router.generate(
                DISTILL_PROMPT.format(topic=topic_str, results=results_text),
                system=DISTILL_SYSTEM,
                temperature=0.2,
                max_tokens=4096,
                **distill_route,
            )
            if resp.success:
                raw_entries = parse_json_array(resp.text) or []
                entries = [e for e in raw_entries if isinstance(e, dict)]
                dropped = len(raw_entries) - len(entries)
                if dropped:
                    # §17.339 — distill LLM occasionally emits a JSON array
                    # of strings (or mixed types) instead of objects. Filter
                    # to dicts so downstream format_toon_rows / ingest_entries
                    # can rely on the documented shape. Drop rather than
                    # coerce: a string entry has no title/source/tags, and
                    # synthesizing those would pollute Milvus with provenance-
                    # free rows.
                    logger.warning(
                        "phase2_distill_shape_drift: job_id=%s raw=%d kept=%d dropped=%d",
                        job_id, len(raw_entries), len(entries), dropped,
                    )
            logger.info(
                "phase2_distill: job_id=%s entry_count=%d", job_id, len(entries),
            )

        # Step 3: TOON + Milvus ingest
        toon_rows = format_toon_rows(entries) if entries else []
        ingest_count = 0
        if entries:
            stats = await ingest_entries(entries, domain=brief.get("domain", "eng"))
            ingest_count = stats["new"] + stats["versioned"]
            logger.info("phase2_ingest: job_id=%s stats=%s", job_id, stats)

        # Optional GitHub push
        gh_result = None
        if push_to_github and toon_rows:
            topic_id = detect_topic_id(brief.get("title", ""), TOPIC_KEYWORDS, default=1)
            target_file = f"knowledge/{TOPIC_MAP.get(topic_id, 'llm-research')}.toon"
            gh_result = await gt_push_to_github(toon_rows, target_file, brief.get("title", ""))

        # Step 4: Compile (router/4b) — user_feedback injected explicitly
        feedback_section = (
            f"\n\nUSER FEEDBACK (must be honored):\n{user_feedback}"
            if user_feedback else ""
        )
        compile_context = json.dumps(
            {
                "brief": brief,
                "user_feedback": user_feedback or "",
                "researched_facts": [e.get("content", "") for e in entries[:10]],
                "fact_count": len(entries),
            },
            indent=2,
        )
        compile_route = (
            {"model": model} if model
            else {"role": settings.ideation_model_role, "overrides": model_overrides}
        )
        resp = await model_router.generate(
            "Compile an execution plan from this context:\n"
            + compile_context
            + feedback_section,
            system=COMPILE_SYSTEM,
            temperature=0.3,
            max_tokens=4096,
            **compile_route,
        )

        workflow = parse_json_object(resp.text) if resp.success else None
        if workflow is None:
            # Compile failure is fatal — do not emit empty workflow_steps.
            # §17.290 — uniform 500 for all in-band Phase 2 failures.
            # Pre-§17.290 this branch returned 502 (Bad Gateway, on the
            # theory that the LLM is upstream). Inconsistent with the
            # generic-exception path below (which `raise`s → FastAPI 500)
            # and operator-orthogonal — no remediation hint in
            # `recovery.py::NEXT_ACTIONS` keys off 502 vs 500, and the
            # `/confirm` re-try path is the same either way. Standardize
            # on 500 so consumers don't need to special-case the code.
            # 404 (job-not-found) and 409 (status-conflict) above stay —
            # those are genuinely client-error semantics.
            err = f"compile step failed (llm_success={resp.success}): {getattr(resp, 'error', None)}"
            logger.error("phase2_compile_failed: job_id=%s error=%s", job_id, err)
            async with async_session() as fail_db:
                await _fail_job(fail_db, job_id, err)
            return {
                "job_id": job_id,
                "status": "failed",
                "error": err,
                "http_status": 500,
            }

    except asyncio.CancelledError:
        logger.warning(
            "phase2_cancelled: job_id=%s reason=client_disconnect", job_id,
        )
        async with async_session() as cancel_db:
            await _cancel_job(cancel_db, job_id, "client_disconnect")
        raise
    except Exception as e:
        err = f"phase2 exception: {e}"
        logger.exception("phase2_unhandled_exception: job_id=%s", job_id)
        async with async_session() as fail_db:
            await _fail_job(fail_db, job_id, err)
        raise

    # Step 5: Persist + transition (short-lived session)
    async with async_session() as write_db:
        await write_db.execute(
            text(
                "UPDATE jobs SET status = 'planning', "
                "research_data = :data, workflow_summary = :workflow "
                "WHERE id = :id"
            ),
            {
                "data": json.dumps(
                    {
                        "feasibility": feasibility,
                        "brief": brief,
                        "research_entries": len(entries),
                        "milvus_ingested": ingest_count,
                    }
                ),
                "workflow": json.dumps(workflow),
                "id": job_id,
            },
        )
        await write_db.commit()

    logger.info(
        "phase2_complete: job_id=%s queries_run=%d results_found=%d "
        "facts_extracted=%d milvus_ingested=%d",
        job_id, len(queries[:query_cap]), len(all_results),
        len(entries), ingest_count,
    )
    return {
        "job_id": job_id,
        "status": "planning",
        "research_summary": {
            "queries_run": len(queries[:query_cap]),
            "results_found": len(all_results),
            "facts_extracted": len(entries),
            "milvus_ingested": ingest_count,
            "toon_rows": len(toon_rows),
            "github": gh_result,
        },
        "workflow": workflow,
        "message": (
            "Research complete. Job is now in 'planning' status. "
            "DAG generation can proceed via /dag or auto-chain."
        ),
    }


async def _cancel_job(db: AsyncSession, job_id: str, reason: str) -> None:
    """Mark a job as cancelled (used for client_disconnect during Phase 2)."""
    await db.execute(
        text(
            "UPDATE jobs SET status = 'cancelled', error_summary = :err "
            "WHERE id = :id"
        ),
        {"err": reason, "id": job_id},
    )
    await db.commit()

