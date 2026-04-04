"""Scaffold Engine — Idea refinement module.

Takes raw idea text → LLM analysis → structured brief.
Persists to jobs table with status transitions:
  pending → refining → planning (ready for DAG generation)

Step 10 of 23-step build plan.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.schemas import JobCreate, JobRead

logger = logging.getLogger("scaffold.refine")

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

async def refine_idea(
    idea_text: str,
    db: AsyncSession,
    model: str | None = None,
    domain: str | None = None,
) -> dict:
    """Refine raw idea text into a structured brief and persist as a job.

    Returns dict with job_id, status, and refined_brief.
    """
    # 1. Create job record in pending state
    result = await db.execute(
        text("""
            INSERT INTO jobs (title, input_text, status)
            VALUES (:title, :input_text, 'pending')
            RETURNING id
        """),
        {"title": idea_text[:80], "input_text": idea_text},
    )
    job_id = result.scalar_one()
    logger.info("job_created: job=%s", job_id)

    # 2. Transition to refining
    await db.execute(
        text("UPDATE jobs SET status = 'refining' WHERE id = :id"),
        {"id": job_id},
    )
    await db.commit()

    # 3. Call LLM for structured brief
    prompt = REFINE_PROMPT.format(idea=idea_text)
    resp = await model_router.generate(
        prompt,
        model=model or model_router.settings.model_general,
        system=REFINE_SYSTEM,
        temperature=0.3,
        max_tokens=2048,
    )

    if not resp.success:
        await _fail_job(db, job_id, f"LLM refinement failed: {resp.error}")
        return {
            "job_id": str(job_id),
            "status": "failed",
            "error": resp.error,
        }

    # 4. Parse LLM output
    brief = _parse_brief(resp.text)
    if brief is None:
        await _fail_job(db, job_id, f"Failed to parse LLM output as JSON")
        return {
            "job_id": str(job_id),
            "status": "failed",
            "error": "LLM output was not valid JSON",
            "raw_output": resp.text[:500],
        }

    # 4b. Override domain if user supplied one
    if domain:
        brief["domain"] = domain

    # 5. Update job with refined brief, transition to planning
    title = brief.get("title", idea_text[:80])
    await db.execute(
        text("""
            UPDATE jobs
            SET title = :title,
                refined_brief = :brief,
                status = 'planning'
            WHERE id = :id
        """),
        {
            "title": title,
            "brief": json.dumps(brief),
            "id": job_id,
        },
    )
    await db.commit()
    logger.info("job_refined: job=%s", job_id)

    return {
        "job_id": str(job_id),
        "status": "planning",
        "refined_brief": brief,
        "model_used": resp.model,
        "duration_ms": resp.total_duration_ms,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_brief(raw: str) -> dict | None:
    """Extract JSON from LLM output, handling common formatting issues."""
    text = raw.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting first JSON object
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start : brace_end + 1])
        except json.JSONDecodeError:
            pass

    return None


async def _fail_job(db: AsyncSession, job_id: UUID, error: str) -> None:
    """Mark job as failed with error summary."""
    await db.execute(
        text("""
            UPDATE jobs
            SET status = 'failed', error_summary = :error
            WHERE id = :id
        """),
        {"error": error[:1000], "id": job_id},
    )
    await db.commit()
    logger.error("job_failed: job=%s error=%s", job_id, error)
