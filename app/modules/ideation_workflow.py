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
    TOPIC_KEYWORDS,
    TOPIC_MAP,
    distill_entries,
    format_toon_rows,
    push_to_github as gt_push_to_github,
    search_searxng,
)
from app.modules.idea_refinement import create_ideation_job, refine_idea
from app.modules.rag_pipeline import ingest_entries
from app.providers.base import Tool
from app.utils.job_utils import fail_job as _fail_job
from app.utils.llm_retry import tool_call_until_args
from app.utils.tool_call_args import read_tool_args
from app.utils.topic_detection import detect_topic_id

logger = logging.getLogger("scaffold.ideation")


# §17.442 — global ideation concurrency cap. Mirrors execution_agent's
# _execution_slot_sem (single-process asyncio.Semaphore; if this ever goes
# multi-worker the cap must move to Redis). Acquired by the /ideas + /ideate
# router handlers so a burst of ideation requests queues instead of all hitting
# the cloud at once (§17.441 stress finding #4). Lazy-init so a test overriding
# settings.ideation_global_concurrency takes effect via _reset_ideation_slot_sem.
_ideation_slot_sem: asyncio.Semaphore | None = None


def get_ideation_slot_sem() -> asyncio.Semaphore:
    global _ideation_slot_sem
    if _ideation_slot_sem is None:
        _ideation_slot_sem = asyncio.Semaphore(settings.ideation_global_concurrency)
    return _ideation_slot_sem


def _reset_ideation_slot_sem() -> None:
    """Test hook — drop the cached semaphore so the next call re-reads settings."""
    global _ideation_slot_sem
    _ideation_slot_sem = None


# §17.454 — strong refs to in-flight async-kickoff Phase 1 tasks. asyncio.create_task
# only holds a weak ref, so without this the GC could collect a task mid-refinement
# and silently strand its job in 'refining'. Mirrors assist_replan._BACKGROUND_TASKS.
_PHASE1_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def run_phase1_in_background(
    job_id: str,
    idea_text: str,
    *,
    model: str | None = None,
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> None:
    """§17.454 — Run Phase 1 (``analyze_and_confirm``) against a pre-created job row
    on its OWN db session.

    The request session that created the row (in ``create_ideation_job``) is torn
    down once ``/ideate/start`` returns, so this opens a fresh session. Any
    unhandled error marks the job ``failed`` with a reason — otherwise the row
    would sit in ``refining`` forever and the detail page could not explain why.
    The ideation concurrency cap is honoured here too (§17.442).
    """
    async with async_session() as db:
        try:
            async with get_ideation_slot_sem():
                await analyze_and_confirm(
                    idea_text, db,
                    model=model, domain=domain,
                    model_overrides=model_overrides, job_id=job_id,
                )
        except Exception as e:
            logger.exception("phase1_background_failed: job=%s", job_id)
            try:
                await _fail_job(db, job_id, f"Phase 1 background error: {e}")
            except Exception:
                logger.exception(
                    "phase1_background_fail_mark_failed: job=%s", job_id,
                )


def spawn_phase1_background(
    job_id: str,
    idea_text: str,
    *,
    model: str | None = None,
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> asyncio.Task:
    """§17.454 — Fire-and-forget the Phase 1 background task with a strong ref so it
    survives GC, plus a done-callback to release the ref on completion."""
    task = asyncio.create_task(
        run_phase1_in_background(
            job_id, idea_text,
            model=model, domain=domain, model_overrides=model_overrides,
        )
    )
    _PHASE1_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_PHASE1_BACKGROUND_TASKS.discard)
    return task


FEASIBILITY_SYSTEM = (
    "You are a technical feasibility analyst. Given a structured brief, assess:\n"
    "1. Is this achievable with local CPU-only LLM infrastructure (Ollama, Milvus, SearXNG)?\n"
    "2. What are the risks or unknowns?\n"
    "3. What clarifications would improve the plan?\n\n"
    "Emit your assessment via the emit_feasibility_assessment tool."
)

# §17.580 — native tool-call schema for the feasibility pass. Mirrors
# idea_refinement.REFINE_BRIEF_TOOL: model_router.tool_call parses structured
# args on native-tool providers and falls back to JSON-coaxing internally on
# non-native providers (e.g. the qwen3.5:397b-cloud reasoning model routed
# through model_general), so this pass no longer silently falls back when the
# model emits <think> prose instead of a bare JSON object. Replaces the legacy
# generate() + parse_json_object() path that assumed clean JSON text.
FEASIBILITY_TOOL = Tool(
    name="emit_feasibility_assessment",
    description=(
        "Emit a structured technical-feasibility assessment for a refined brief."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "feasible": {
                "type": "boolean",
                "description": "Whether the idea is achievable on local CPU-only infra",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the assessment, 0.0-1.0",
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Risks or unknowns",
            },
            "clarifications_needed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Questions that would improve the plan",
            },
            "recommended_research_queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Search queries to ground the plan in Phase 2",
            },
            "summary": {
                "type": "string",
                "description": "One-paragraph assessment",
            },
        },
        "required": ["feasible", "confidence", "summary"],
    },
)

COMPILE_SYSTEM = (
    "You are a prompt architect. Given a refined brief and researched facts, produce:\n"
    "1. An optimal prompt for executing this idea via a DAG of LLM nodes\n"
    "2. A step-by-step workflow the user should follow\n\n"
    "Emit the plan via the emit_execution_plan tool."
)

# §17.581 — native tool-call schema for the Phase-2 compile pass. Same fix as
# §17.580's feasibility tool: model_general → qwen3.5:397b-cloud is a reasoning
# model, and the legacy generate() + parse_json_object() path silently produced
# a compile failure whenever it emitted <think> prose. model_router.tool_call
# parses structured args natively / via coaxing; tool_call_until_args re-draws
# on the empty-args thinking-model variance (compile failure is fatal).
COMPILE_TOOL = Tool(
    name="emit_execution_plan",
    description=(
        "Emit an execution plan (DAG-ready prompt + workflow steps) for a brief."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "compiled_prompt": {
                "type": "string",
                "description": "Full prompt text ready for DAG generation",
            },
            "workflow_steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "integer"},
                        "action": {"type": "string"},
                        "tool": {
                            "type": "string",
                            "enum": ["LLM", "CodeGen", "SearXNG", "Milvus", "Shell"],
                        },
                        "notes": {"type": "string"},
                    },
                    "required": ["step", "action"],
                },
                "description": "Ordered steps the user should follow",
            },
            "configuration": {
                "type": "object",
                "properties": {
                    "temperature": {"type": "number"},
                    "domain": {
                        "type": "string",
                        "enum": ["prompt", "rag", "llm", "spec", "eng"],
                    },
                    "estimated_nodes": {"type": "integer"},
                },
                "description": "Suggested DAG configuration",
            },
        },
        "required": ["compiled_prompt", "workflow_steps"],
    },
)


async def analyze_and_confirm(
    idea_text: str,
    db: AsyncSession,
    model: str | None = None,
    domain: str | None = None,
    model_overrides: dict | None = None,
    job_id: str | None = None,
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
        job_id=job_id,
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
    resp = await model_router.tool_call(
        messages=[
            {"role": "system", "content": FEASIBILITY_SYSTEM},
            {
                "role": "user",
                "content": "Assess this brief:\n" + json.dumps(brief, indent=2),
            },
        ],
        tools=[FEASIBILITY_TOOL],
        temperature=0.2,
        # §17.580 — generous ceiling: model_general routes to the qwen3.5
        # reasoning model, which spends tokens on <think> before emitting the
        # tool call; 2048 truncated it pre-fix. Matches the tool_call default.
        max_tokens=4096,
        **route_kwargs,
    )

    feasibility = read_tool_args(resp)
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

        # Step 2: LLM distillation (router/4b) via the shared native-tool-call
        # primitive. §17.x — the legacy generate + parse_json_array path here
        # dropped 100% of results (phase2_distill_shape_drift: raw=10 kept=0
        # dropped=10) because DISTILL_SYSTEM no longer carries an object-shape
        # spec — that moved into RECORD_DISTILLED_ENTRIES_TOOL when gt_extractor
        # switched to native tool-calls, but this path was never migrated. The
        # 4b model returned an array of strings and the §17.339 dict-filter
        # discarded all of them. distill_entries forces the object shape via the
        # tool schema (and keeps the §17.464 fail-soft-on-empty contract).
        entries: list[dict] = []
        distill_cap = settings.ideation_max_distill_results
        if all_results:
            distill_route = (
                {"model": model} if model
                else {"role": settings.ideation_model_role, "overrides": model_overrides}
            )
            entries = await distill_entries(
                all_results,
                topic=brief.get("title", "unknown"),
                route=distill_route,
                max_results=distill_cap,
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
        # §17.581 — native tool-call compile (was generate + parse_json_object).
        # model_general → qwen3.5:397b-cloud emits <think> prose that
        # parse_json_object couldn't read, silently producing "research
        # completed but compile failed" (the twin of the §17.463 DAG bug and
        # §17.580's feasibility bug, one step later). tool_call reads structured
        # args; tool_call_until_args keeps the §17.464 retry-on-empty guard —
        # here it re-draws on the empty-ARGS thinking-model variance, with
        # 8192-token headroom. Compile failure is fatal, so the guard matters.
        resp = await tool_call_until_args(
            model_router.tool_call,
            [
                {"role": "system", "content": COMPILE_SYSTEM},
                {
                    "role": "user",
                    "content": "Compile an execution plan from this context:\n"
                    + compile_context
                    + feedback_section,
                },
            ],
            [COMPILE_TOOL],
            compile_route,
            temperature=0.3,
            max_tokens=8192,
            label="phase2_compile",
        )

        workflow = read_tool_args(resp)
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

