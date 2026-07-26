"""Scaffold Engine — Idea refinement module.

Takes raw idea text → LLM analysis → structured brief.
Persists to jobs table with status transitions:
  pending → refining → planning (ready for DAG generation)

Step 10 of 23-step build plan.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.providers.base import Tool
from app.utils.job_utils import fail_job as _fail_job
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.refine")

ALLOWED_DOMAINS = {"prompt", "rag", "llm", "spec", "eng", "eng_design"}  # §17.330 — eng_design closes the §17.329 gap; /ideate must accept it as a domain override
# §17.515 — domains the refinement LLM may auto-select. eng_design is the
# circuits/EDA partition (deliberate-write-only, "no classifier route" per the
# eng/eng_design split); it must stay an explicit override only, NOT something
# the model picks — otherwise software tasks like "blue-green DEPLOYMENT" leak
# into it on the "design" keyword and then get empty/wrong RAG grounding at
# execution. It remains in ALLOWED_DOMAINS so an explicit override is accepted.
LLM_SELECTABLE_DOMAINS = ALLOWED_DOMAINS - {"eng_design"}

# ---------------------------------------------------------------------------
# Refinement prompt + tool schema (Sprint X.11)
# ---------------------------------------------------------------------------

REFINE_SYSTEM = """You are a workflow planning assistant. Given a raw idea, produce a structured brief.

Rules:
- Extract implicit requirements from the idea
- If the idea is vague, list ambiguities but still produce a best-effort brief
- Keep goals specific and actionable
- No filler words, no hedging

§17.649 — Non-destructive default (READ CAREFULLY). When the idea refers to an
EXISTING or already-running system — signalled by words like "existing",
"already", "current", "my <server/host/cluster/database>", "on my …", or a
named service that is clearly in use — and asks to CLEAN UP, MODIFY, ADD TO, or
RECONFIGURE it (e.g. "remove old data", "clean up", "reconfigure", "add X",
"harden"), you MUST NOT escalate that into destroying the system:
  * Do NOT turn "remove old data" into "wipe all storage / all disks".
  * Do NOT turn "set up / configure X on my existing Proxmox" into "reinstall the
    OS" or "provision the host from scratch".
  * Do NOT bake a full wipe, OS reinstall, reformat, or delete-everything action
    into `goals` to resolve an uncertainty about the system's current state.
Prefer the CONSERVATIVE, in-place interpretation that PRESERVES the existing
system and touches only what the idea names (the old data, the specific service,
the specific config). A destructive whole-system action (wipe all disks,
reinstall the OS, reformat, drop the database, factory-reset) belongs in `goals`
ONLY when the idea UNAMBIGUOUSLY asks to rebuild/reprovision from scratch (e.g.
"fresh install", "bare-metal rebuild", "start over", "reformat everything").
If whether to rebuild-from-scratch vs. modify-in-place is genuinely unclear,
record it in `ambiguities` AND choose the non-destructive reading for `goals` —
never the reverse. Removing DATA is not the same as wiping DISKS or reinstalling
an OS; keep them distinct."""

REFINE_PROMPT = """Analyze this idea and produce a structured brief:

---
{idea}
---"""

# Sprint X.11 — native tool-call schema. The wrapper (model_router.tool_call)
# parses structured args on native-tool providers and falls back to JSON-
# coaxing internally on non-native providers, so callers always read via
# resp.tool_calls[0].arguments. Replaces the legacy "OUTPUT FORMAT (strict
# JSON, no markdown fences):..." prose block in REFINE_SYSTEM.
REFINE_BRIEF_TOOL = Tool(
    name="emit_refined_brief",
    description=(
        "Emit a structured planning brief extracted from the raw idea text."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Concise title (max 8 words)",
            },
            "description": {
                "type": "string",
                "description": "One paragraph describing the goal",
            },
            "domain": {
                "type": "string",
                "enum": sorted(LLM_SELECTABLE_DOMAINS),
                "description": (
                    "Knowledge domain for retrieval grounding. "
                    "'eng' = software engineering — the default for code, "
                    "infrastructure, devops, deployment, CLI tools, servers. "
                    "'llm' = LLM/ML/model work. 'rag' = retrieval/search/"
                    "embeddings. 'prompt' = prompt engineering. 'spec' = specs/"
                    "standards/protocols. When unsure, choose 'eng'."
                ),
            },
            "goals": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Specific measurable outcomes. §17.649 — for an existing/"
                    "already-running system, keep goals non-destructive and "
                    "in-place: 'remove old data' ≠ 'wipe all disks'; 'configure X "
                    "on existing Proxmox' ≠ 'reinstall the OS'. Only include a "
                    "full wipe / OS reinstall / reformat / delete-all goal when "
                    "the idea unambiguously asks to rebuild from scratch."
                ),
            },
            "constraints": {
                "type": "array",
                "items": {"type": "string"},
            },
            "inputs_available": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What the user already has",
            },
            "outputs_expected": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What the user wants produced",
            },
            "complexity": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "ambiguities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Anything unclear that may need clarification",
            },
        },
        "required": ["title", "description", "domain", "goals", "complexity"],
    },
)


# ---------------------------------------------------------------------------
# Core refinement logic
# ---------------------------------------------------------------------------


def _truncate_title(text_in: str, max_chars: int = 80) -> str:
    """Trim a title to ``max_chars`` at a word boundary, appending an ellipsis.

    Returns ``text_in`` unchanged if it already fits. If trimming lands
    inside a word, walks back to the previous space; if there is no space
    in the budget, falls back to a hard cut. The ellipsis (…) is
    counted against ``max_chars``.
    """
    t = (text_in or "").strip()
    if len(t) <= max_chars:
        return t
    budget = max_chars - 1  # reserve 1 char for the ellipsis
    cut = t[:budget]
    space = cut.rfind(" ")
    if space >= max_chars // 2:  # only walk back if it's not absurdly short
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"

async def create_ideation_job(
    idea_text: str,
    db: AsyncSession,
    domain: str | None = None,
) -> str:
    """§17.454 — Insert the job row in ``refining`` and return its id immediately,
    BEFORE the 100-547s Phase 1 LLM pass runs.

    The async-kickoff endpoint (``POST /ideate/start``) returns this id so the
    native web UI can redirect straight to the live job-detail page instead of
    making the user hunt for their just-submitted job in a filtered list. The
    actual refinement runs afterwards in a background task that re-attaches to
    this same row via ``refine_idea(..., job_id=...)``.
    """
    if domain is not None and domain not in ALLOWED_DOMAINS:
        raise ValueError(
            f"invalid domain override {domain!r}; allowed: {sorted(ALLOWED_DOMAINS)}"
        )
    result = await db.execute(
        text("""
            INSERT INTO jobs (title, input_text, status)
            VALUES (:title, :input_text, 'refining')
            RETURNING id
        """),
        {"title": _truncate_title(idea_text), "input_text": idea_text},
    )
    job_id = result.scalar_one()
    await db.commit()
    logger.info("ideation_job_created_async: job=%s status=refining", job_id)
    return str(job_id)


async def refine_idea(
    idea_text: str,
    db: AsyncSession,
    model: str | None = None,
    domain: str | None = None,
    model_overrides: dict | None = None,
    target_status: str = "awaiting_confirmation",
    job_id: str | None = None,
) -> dict:
    """Refine raw idea text into a structured brief and persist as a job.

    Returns dict with job_id, status, and refined_brief.

    §17.454 — when ``job_id`` is supplied the INSERT is skipped and the existing
    ``refining`` row (pre-created by :func:`create_ideation_job`) is reused. This
    is the async-kickoff path; the synchronous callers (``/ideas``, ``/ideate``,
    chat pipeline) pass ``job_id=None`` and create the row here as before.
    """
    # 0. Validate domain override if supplied
    if domain is not None and domain not in ALLOWED_DOMAINS:
        raise ValueError(
            f"invalid domain override {domain!r}; allowed: {sorted(ALLOWED_DOMAINS)}"
        )

    # 1. Create job directly in 'refining' state (single INSERT, single commit),
    #    OR reuse a row pre-created by create_ideation_job (async-kickoff path).
    if job_id is None:
        result = await db.execute(
            text("""
                INSERT INTO jobs (title, input_text, status)
                VALUES (:title, :input_text, 'refining')
                RETURNING id
            """),
            {"title": _truncate_title(idea_text), "input_text": idea_text},
        )
        job_id = result.scalar_one()
        await db.commit()
        logger.info("job_created: job=%s status=refining", job_id)
    else:
        logger.info("job_reused: job=%s status=refining", job_id)

    # 2. Call LLM for structured brief via native tool-call (Sprint X.11)
    prompt = REFINE_PROMPT.format(idea=idea_text)
    route_kwargs = (
        {"model": model} if model
        else {"role": "model_general", "overrides": model_overrides}
    )
    try:
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": REFINE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            tools=[REFINE_BRIEF_TOOL],
            temperature=0.3,
            max_tokens=2048,
            **route_kwargs,
        )
    except Exception as e:
        await _fail_job(db, job_id, f"LLM refinement exception: {e}")
        raise

    if not resp.success:
        await _fail_job(db, job_id, f"LLM refinement failed: {resp.error}")
        return {
            "job_id": str(job_id),
            "status": "failed",
            "error": resp.error,
        }

    # 3. Read structured args (Sprint X.11). The wrapper handles both native
    # tool-call providers (args parsed by the SDK) and coaxing fallbacks
    # (args parsed from JSON-text by model_router._tool_call_via_coaxing),
    # so this read path is provider-agnostic.
    brief = read_tool_args(resp)
    if brief is None:
        await _fail_job(db, job_id, "Failed to parse refined brief from tool_call")
        return {
            "job_id": str(job_id),
            "status": "failed",
            "error": "LLM did not produce a valid refined brief",
            "raw_output": (resp.text or "")[:500],
        }

    # 3b. Override domain if user supplied one
    if domain:
        brief["domain"] = domain

    # 4. Update job with refined brief, transition to planning
    title = _truncate_title(brief.get("title") or idea_text)
    await db.execute(
        text("""
            UPDATE jobs
            SET title = :title,
                refined_brief = :brief,
                status = :target_status
            WHERE id = :id
        """),
        {
            "title": title,
            "brief": json.dumps(brief),
            "id": job_id,
            "target_status": target_status,
        },
    )
    await db.commit()
    logger.info("job_refined: job=%s", job_id)

    return {
        "job_id": str(job_id),
        "status": target_status,
        "refined_brief": brief,
        "model_used": resp.model,
        "duration_ms": resp.total_duration_ms,
    }


