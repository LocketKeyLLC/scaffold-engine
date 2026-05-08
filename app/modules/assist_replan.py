"""Assist Mode re-plan strategies.

Decides what to do when human-supplied output diverges from what the
autonomous executor would have produced. Three policies, configured
per-session on `assist_sessions.replan_policy`:

  context_only (DEFAULT) — no regeneration. Human evidence lands in
    `dag_nodes.output_text`; the existing upstream-last assembly forces
    downstream nodes to "build on" the actual upstream output. Handles
    most divergence implicitly; zero LLM cost.

  selective                — regenerate only nodes that transitively depend
    on the changed node. Reuses the BFS in retry_failed_node.
    One LLM call, scoped to the affected subgraph.

  full                     — regenerate all pending nodes. Discouraged;
    invalidates trust mid-session.

  disabled                 — skip detection entirely.

The verifier is the same `model_verifier` (qwen2.5:7b) the autonomous
executor uses post-LLM, preserving the model-stack invariant.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold.assist.replan")


_DIVERGENCE_PROMPT = """You are checking whether a human-supplied step output \
diverges from the task description.

TASK TITLE: {title}
TASK PROMPT: {prompt}

HUMAN OUTPUT (just submitted):
{evidence}

Decide: does the human output meet the task's intent?
- Trivially-different wording, formatting, or order is NOT divergence.
- A different deliverable type, missing required content, or pivoting to a
  different solution path IS divergence.

Respond with a single JSON object, no prose:
{{"diverges": true|false, "severity": "minor"|"major", "reason": "<short>"}}"""


async def detect_divergence(
    *, title: str, prompt: str, evidence: str, model_overrides: dict | None = None,
) -> dict:
    """Run the divergence verifier. Returns the parsed JSON dict.

    Returns `{diverges: False, severity: 'minor', reason: 'detection_unavailable'}`
    on any failure (parse error, model unavailable) — assist mode must
    not block on a flaky detector. Logs the failure for observability.
    """
    # Defer the model_router import; it pulls heavy http client state.
    from app import model_router
    from app.config import settings
    overrides = model_overrides or {}
    model = overrides.get("model_verifier") or settings.model_verifier
    msg = _DIVERGENCE_PROMPT.format(
        title=title or "(untitled)",
        prompt=(prompt or "")[:4000],
        evidence=(evidence or "")[:4000],
    )
    try:
        resp = await model_router.chat(
            messages=[{"role": "user", "content": msg}],
            model=model,
            max_tokens=200,
        )
        raw = getattr(resp, "text", "") or ""
    except Exception as e:
        logger.warning("divergence_detector_failed: %s", e)
        return {"diverges": False, "severity": "minor", "reason": "detection_unavailable"}
    parsed = parse_json_object(raw)
    if not parsed or "diverges" not in parsed:
        logger.warning("divergence_detector_unparsed: raw=%r", (raw or "")[:200])
        return {"diverges": False, "severity": "minor", "reason": "detection_unparsed"}
    return {
        "diverges": bool(parsed.get("diverges")),
        "severity": parsed.get("severity", "minor"),
        "reason": parsed.get("reason", "")[:200],
    }


# ── Subgraph helpers ───────────────────────────────────────────────────────


async def downstream_node_keys(*, db, job_id: str, root_node_key: str) -> list[str]:
    """BFS the dependents of a node within a job's DAG.

    Returns node_keys that transitively depend on `root_node_key`,
    excluding the root itself. Empty list if no dependents.
    """
    rows = (await db.execute(
        text("""
            SELECT node_key, depends_on FROM dag_nodes WHERE job_id = :jid
        """),
        {"jid": job_id},
    )).mappings().all()
    succ: dict[str, list[str]] = {}
    for r in rows:
        for dep in (r["depends_on"] or []):
            succ.setdefault(dep, []).append(r["node_key"])
    seen: set[str] = set()
    queue = list(succ.get(root_node_key, []))
    while queue:
        nk = queue.pop(0)
        if nk in seen:
            continue
        seen.add(nk)
        queue.extend(succ.get(nk, []))
    return sorted(seen)


# ── Selective re-plan ──────────────────────────────────────────────────────


async def apply_selective_replan(
    *,
    db,
    session_id: str,
    job_id: str,
    root_node_key: str,
    root_evidence: str,
    divergence: dict,
    model_overrides: dict | None = None,
) -> dict:
    """For policy='selective': identify the subgraph that depends on the
    changed node, regenerate prompt_template for those nodes via LLM
    (Sprint W.5), and reset their assist_steps + dag_nodes to pending so
    the user (or autonomous handoff) can re-walk them.

    The DAG topology stays the same — what changed is the *upstream
    context*. Two layers of compensation now apply:

      1. Sprint W.5 — `dag_generator.regenerate_subgraph` rewrites each
         affected node's short execution hint (``prompt_template``) so
         it aligns with the new root output. Fail-open: any LLM/parse
         failure logs a warning and falls back to legacy behavior.
      2. Existing — at execution time, ``_build_prompt`` injects the
         fresh upstream output into the assembled prompt regardless of
         the hint. So even if regen returns nothing, the next walk
         still sees the new upstream.

    Returns: {affected_nodes, scope, regenerated_count, regen_errors,
              severity, reason}.
    """
    affected = await downstream_node_keys(db=db, job_id=job_id, root_node_key=root_node_key)
    if not affected:
        return {"affected_nodes": [], "scope": "selective", "details": "no_dependents"}

    # Sprint W.5 — regenerate prompt templates BEFORE the reset so that if
    # regen fails (fail-open), we still preserve the legacy reset-only
    # behavior. The reset clears status/output_text/timestamps but keeps
    # whatever prompt_template the regen produced (or the original, on
    # fail-open).
    from app.modules.dag_generator import regenerate_subgraph
    regen_result = await regenerate_subgraph(
        job_id=job_id,
        root_node_key=root_node_key,
        root_evidence=root_evidence,
        affected_keys=affected,
        db=db,
        model_overrides=model_overrides,
    )

    # Reset only nodes that are NOT already terminal-by-skip.
    await db.execute(
        text("""
            UPDATE dag_nodes
               SET status = 'pending',
                   output_text = NULL,
                   completed_at = NULL,
                   updated_at = NOW()
             WHERE job_id = :jid
               AND node_key = ANY(:keys)
               AND status NOT IN ('skipped', 'pending')
        """),
        {"jid": job_id, "keys": affected},
    )
    await db.execute(
        text("""
            UPDATE assist_steps
               SET status = 'pending',
                   evidence = NULL,
                   evidence_kind = NULL,
                   evidence_meta = '{}'::jsonb,
                   submitted_at = NULL,
                   committed_at = NULL,
                   replan_triggered = TRUE,
                   updated_at = NOW()
             WHERE session_id = :sid
               AND node_key = ANY(:keys)
               AND status NOT IN ('skipped',)
        """),
        {"sid": session_id, "keys": affected},
    )
    await db.commit()
    logger.info(
        "assist_selective_replan session_id=%s root=%s affected=%d severity=%s",
        session_id, root_node_key, len(affected), divergence.get("severity"),
    )
    return {
        "affected_nodes": affected,
        "scope": "selective",
        "severity": divergence.get("severity"),
        "reason": divergence.get("reason"),
        "regenerated_count": regen_result.get("regenerated", 0),
        "regen_errors": regen_result.get("errors", []),
    }


# ── Top-level dispatcher ───────────────────────────────────────────────────


async def maybe_replan(
    *,
    db,
    session_id: str,
    job_id: str,
    node_key: str,
    title: str,
    prompt: str,
    evidence: str,
    policy: str,
    model_overrides: dict | None = None,
) -> dict | None:
    """Run divergence detection + apply policy.

    Returns None when no replan was triggered (policy='disabled', or
    divergence not detected, or context_only). Returns a dict when an
    actual reset happened (policy='selective' / 'full').
    """
    if policy == "disabled":
        return None
    div = await detect_divergence(
        title=title, prompt=prompt, evidence=evidence,
        model_overrides=model_overrides,
    )
    if not div["diverges"] or div["severity"] != "major":
        return None
    if policy == "context_only":
        # No structural change — log the divergence but rely on
        # downstream nodes' upstream-last assembly to absorb it.
        logger.info(
            "assist_divergence_logged session_id=%s node=%s reason=%r",
            session_id, node_key, div.get("reason"),
        )
        # Mark on the row.
        await db.execute(
            text("""
                UPDATE assist_steps SET divergence = TRUE, updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
            """),
            {"sid": session_id, "nk": node_key},
        )
        await db.commit()
        return None
    if policy == "selective":
        return await apply_selective_replan(
            db=db, session_id=session_id, job_id=job_id,
            root_node_key=node_key, root_evidence=evidence, divergence=div,
            model_overrides=model_overrides,
        )
    if policy == "full":
        # Treat as "select all pending" — implemented via the same
        # selective machinery with the entire pending set.
        return await apply_selective_replan(
            db=db, session_id=session_id, job_id=job_id,
            root_node_key=node_key, root_evidence=evidence, divergence=div,
            model_overrides=model_overrides,
        )
    if policy == "disabled":
        # Operator opted out of replan-on-divergence entirely — no-op
        # but mark the divergence so it's still observable in audit.
        await db.execute(
            text("""
                UPDATE assist_steps SET divergence = TRUE, updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
            """),
            {"sid": session_id, "nk": node_key},
        )
        await db.commit()
        return None
    # The replan_policy column has a CHECK constraint (migration 023)
    # restricting values to context_only/selective/full/disabled. Reaching
    # here means the constraint was bypassed or the row pre-dates the
    # constraint — fail loud so the data is reconciled rather than silently
    # ignored.
    raise ValueError(f"unknown replan_policy: {policy!r}")
