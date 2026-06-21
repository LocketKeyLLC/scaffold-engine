"""§17.576 — learning flywheel (opt-in, default OFF).

Turns the engine's own high-grounding deliverables into retrievable exemplars:
  - ``maybe_ingest_exemplar``: at job completion, if the deliverable's grounding
    score clears ``exemplar_min_grounding``, ingest it into RAG tagged
    ``source_type="exemplar"`` (pollution guarded by the grounding threshold +
    RAG's existing 3-tier dedup).
  - ``retrieve_exemplars``: at DAG-plan time, fetch similar exemplars and format
    them as a few-shot "proven prior solutions" block for the planner prompt.

Both fail-soft (never break completion/planning) and gated default-OFF. Retrieval
post-filters on ``source_type`` rather than touching the RAG retrieval hot path.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger("scaffold.flywheel")

EXEMPLAR_SOURCE_TYPE = "exemplar"


async def maybe_ingest_exemplar(
    *,
    job_id: str,
    compiled_output: str | None,
    deliverable_kind: str | None,
    grounding_score: float | None,
    domain: str = "eng",
) -> bool:
    """Ingest a completed deliverable as a RAG exemplar when it clears the
    grounding bar. Returns True if an ingest was attempted. Default-OFF, fail-soft."""
    if not settings.exemplar_ingest_enabled:
        return False
    if grounding_score is None or grounding_score < settings.exemplar_min_grounding:
        return False
    if (deliverable_kind or "") == "plan_only" or not (compiled_output or "").strip():
        return False  # plans aren't proven solutions; empty output is nothing
    try:
        from app.modules.rag_pipeline import ingest_entries
        await ingest_entries(
            [{
                "title": f"Exemplar — job {job_id[:8]} ({deliverable_kind or 'report'})",
                "canonical_text": compiled_output,
                "domain_tags": [EXEMPLAR_SOURCE_TYPE, deliverable_kind or "report"],
                "source_type": EXEMPLAR_SOURCE_TYPE,
                "source_url": f"job:{job_id}",
                "confidence_score": grounding_score,
                "provenance": {
                    "job_id": job_id, "grounding_score": grounding_score,
                    "deliverable_kind": deliverable_kind,
                },
            }],
            domain=domain or "eng",
        )
        logger.info(
            "exemplar_ingested: job=%s grounding=%.2f kind=%s domain=%s",
            job_id, grounding_score, deliverable_kind, domain,
        )
        return True
    except Exception as exc:  # fail-soft — never break completion
        logger.warning("exemplar_ingest_failed: job=%s err=%s", job_id, exc)
        return False


async def retrieve_exemplars(
    query: str, *, domain: str | None = None, top_k: int | None = None,
) -> str:
    """Return a formatted "proven prior solutions" block from exemplar entries
    similar to ``query``, or "" (none / disabled / error). Over-fetches then
    post-filters on ``source_type`` so the RAG hot path is untouched."""
    if not settings.exemplar_retrieval_enabled or not (query or "").strip():
        return ""
    k = top_k or settings.exemplar_retrieval_top_k
    try:
        from app.modules.rag_pipeline import query_rag
        resp = await query_rag(query, domain=domain, top_k=max(k * 3, k), skip_rerank=False)
        if resp.get("status") != "ok":
            return ""
        ex = [
            r for r in resp.get("results", [])
            if r.get("source_type") == EXEMPLAR_SOURCE_TYPE
        ][:k]
        if not ex:
            return ""
        blocks = []
        for i, r in enumerate(ex, 1):
            score = float(r.get("confidence_score", 0.0) or 0.0)
            body = (r.get("content") or "")[:1500]
            blocks.append(f"### Proven prior solution {i} (grounding {score:.2f})\n{body}")
        return (
            "## Proven prior solutions (high-grounding deliverables from similar "
            "past jobs — use as reference; ADAPT, do not copy):\n"
            + "\n\n".join(blocks)
            + "\n\n---\n\n"
        )
    except Exception as exc:  # fail-soft — never break planning
        logger.warning("exemplar_retrieve_failed: err=%s", exc)
        return ""
