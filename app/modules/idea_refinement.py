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
from app.utils.job_utils import fail_job as _fail_job
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold.refine")

ALLOWED_DOMAINS = {"prompt", "rag", "llm", "spec", "eng"}

# ---------------------------------------------------------------------------
# Refinement prompt
# ---------------------------------------------------------------------------

REFINE_SYSTEM = """You are a workflow planning assistant. Given a raw idea, produce a structured brief.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "title": "concise title (max 8 words)",
  "description": "one paragraph describing the goal",
  "domain": "one of: prompt, rag, llm, spec, eng",
  "goals": ["specific measurable outcome 1", "outcome 2"],
  "constraints": ["constraint 1", "constraint 2"],
  "inputs_available": ["what the user already has"],
  "outputs_expected": ["what the user wants produced"],
  "complexity": "low | medium | high",
  "ambiguities": ["anything unclear that may need clarification"]
}

Rules:
- Extract implicit requirements from the idea
- If the idea is vague, list ambiguities but still produce a best-effort brief
- Keep goals specific and actionable
- No filler words, no hedging"""

REFINE_PROMPT = """Analyze this idea and produce a structured brief:

---
{idea}
---

Return ONLY the JSON object. No preamble, no markdown."""


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

async def refine_idea(
    idea_text: str,
    db: AsyncSession,
    model: str | None = None,
    domain: str | None = None,
    model_overrides: dict | None = None,
    target_status: str = "awaiting_confirmation",
) -> dict:
    """Refine raw idea text into a structured brief and persist as a job.

    Returns dict with job_id, status, and refined_brief.
    """
    # 0. Validate domain override if supplied
    if domain is not None and domain not in ALLOWED_DOMAINS:
        raise ValueError(
            f"invalid domain override {domain!r}; allowed: {sorted(ALLOWED_DOMAINS)}"
        )

    # 1. Create job directly in 'refining' state (single INSERT, single commit)
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

    # 2. Call LLM for structured brief (guarded)
    prompt = REFINE_PROMPT.format(idea=idea_text)
    route_kwargs = (
        {"model": model} if model
        else {"role": "model_general", "overrides": model_overrides}
    )
    try:
        resp = await model_router.generate(
            prompt,
            system=REFINE_SYSTEM,
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

    # 3. Parse LLM output
    brief = parse_json_object(resp.text)
    if brief is None:
        await _fail_job(db, job_id, f"Failed to parse LLM output as JSON")
        return {
            "job_id": str(job_id),
            "status": "failed",
            "error": "LLM output was not valid JSON",
            "raw_output": resp.text[:500],
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


