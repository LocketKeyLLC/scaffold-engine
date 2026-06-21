"""§17.299 — node-retry helpers lifted from execution_agent.py.

§17.280-🟢-4 closeout. The audit flagged execution_agent.py (1736 LOC
at audit time, 1818 by §17.298) as the next-largest production module
with "no specific complaint" — operator-picked to extract the
verifier+retry path so future additions land in a focused module
rather than continuing to grow the hot-path file.

What lives here:

- ``_format_reviewer_feedback(node)`` — builds the "Reviewer feedback
  (attempt N)" block that gets prepended to a node's prompt on retry.
  Gated on ``retry_count > 0`` so a first attempt never sees it. The
  ``_build_prompt`` caller in execution_agent imports this name from
  here on every call.

- ``retry_failed_node(job_id, node_key, db)`` — the ``POST /exec/retry``
  request path. Validates the node is failed with retries remaining,
  walks the DAG to BFS-collect transitive downstream nodes, atomically
  resets the failed node + downstream pendings to pending, flips the
  job from failed/blocked → executing. Used both by the explicit
  retry endpoint AND by ``execute_all_nodes``' auto-retry budget
  (when ``execution_global_retry_cap > 0`` and a node fails mid-stream).

What stayed in execution_agent.py:

- ``_verify_output`` already lives in ``execution_verify.py`` (pre-
  §17.299 extraction).
- The auto-retry budget consumption in ``execute_all_nodes`` (around
  line 1700 of execution_agent.py) stays inline because it's
  interleaved with SSE event emission and control-flow ``continue``
  statements — extracting it would require a callback or generator-
  return-decision protocol that adds indirection without simplifying
  the call site.

Tests that import these names from ``app.modules.execution_agent``
keep working via re-export aliases:

    from app.modules.execution_retry import (
        _format_reviewer_feedback,
        retry_failed_node,
    )

The aliases are identity-equal to the vendor names; a
``tests/test_execution_retry_module.py`` regression guard pins that
contract so a re-inlined body would fail review.
"""
from __future__ import annotations

import logging
from collections import deque

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_model, settings

logger = logging.getLogger(__name__)


def _format_reviewer_feedback(node: dict) -> str:
    """Return a Reviewer-feedback block to prepend on retry, or '' if N/A.

    Gated on ``retry_count > 0`` so a first attempt never sees the block,
    even if a stale reason somehow made it onto the row.
    """
    retry_count = node.get("retry_count") or 0
    reason = (node.get("last_verification_reason") or "").strip()
    if retry_count <= 0 or not reason:
        return ""
    return (
        f"## Reviewer feedback (attempt {retry_count + 1})\n"
        f"The previous attempt was rejected by the verifier. Reason:\n"
        f"  {reason}\n\n"
        f"Address that specifically in your next output. Do not repeat the\n"
        f"prior failure mode.\n\n"
        f"---\n\n"
    )


async def retry_failed_node(job_id: str, node_key: str, db: AsyncSession) -> dict:
    """Reset a failed node to pending and cascade-reset downstream nodes."""
    # ---- Stage 1: Validate ----
    row = (await db.execute(
        text("""
            SELECT node_key, status, retry_count, max_retries
            FROM dag_nodes
            WHERE job_id = :jid AND node_key = :nk
        """),
        {"jid": job_id, "nk": node_key},
    )).fetchone()

    if not row:
        return {"status": "error", "message": "Node %s not found" % node_key}

    if row.status != "failed":
        return {
            "status": "error",
            "message": "Node %s is '%s', not 'failed'" % (node_key, row.status),
        }

    if row.retry_count >= row.max_retries:
        # §17.577 — final escalation rung: hand the job to Assist Mode (human)
        # instead of just failing. Opt-in + fail-soft (falls through to the
        # normal exhausted error if assist can't start).
        if settings.node_escalation_enabled and settings.node_escalation_to_assist:
            try:
                from app.modules.assist_agent import start_assist_session
                await start_assist_session(job_id=job_id, db=db)
                logger.info(
                    "node_escalated_to_assist job=%s node=%s retries=%d/%d",
                    job_id, node_key, row.retry_count, row.max_retries,
                )
                return {"status": "escalated_to_assist", "node_key": node_key}
            except Exception as exc:  # fail-soft
                logger.warning("escalate_to_assist_failed job=%s err=%s", job_id, exc)
        return {
            "status": "error",
            "message": "Node %s exhausted retries (%d/%d)" % (
                node_key, row.retry_count, row.max_retries
            ),
        }

    new_retry_count = row.retry_count + 1

    # §17.577 — adaptive escalation: pick the model rung for THIS retry
    # (retry 1 → order[0], clamped to the last rung). Set on the node row in
    # Stage 5 so both serial + parallel re-execution honor it via assigned_model.
    escalation_model: str | None = None
    if settings.node_escalation_enabled and settings.node_escalation_order:
        order = settings.node_escalation_order
        rung_role = order[min(new_retry_count - 1, len(order) - 1)]
        try:
            escalation_model = get_model(rung_role)
        except Exception as exc:  # fail-soft — unknown role → no escalation
            logger.warning("escalation_model_resolve_failed: rung=%s err=%s", rung_role, exc)

    # ---- Stage 2: Load full DAG topology ----
    all_rows = (await db.execute(
        text("""
            SELECT node_key, status, depends_on
            FROM dag_nodes
            WHERE job_id = :jid
        """),
        {"jid": job_id},
    )).fetchall()

    # ---- Stage 3: Build reverse adjacency map ----
    downstream_map: dict[str, set[str]] = {}
    for r in all_rows:
        for parent_key in (r.depends_on or []):
            downstream_map.setdefault(parent_key, set()).add(r.node_key)

    # ---- Stage 4: BFS for transitive downstream nodes ----
    queue = deque(downstream_map.get(node_key, set()))
    visited: set[str] = set()
    while queue:
        nk = queue.popleft()
        if nk in visited:
            continue
        visited.add(nk)
        queue.extend(downstream_map.get(nk, set()))

    status_lookup = {r.node_key: r.status for r in all_rows}
    downstream_to_reset = [
        nk for nk in visited
        if status_lookup.get(nk) in ("pending", "failed")
    ]

    # ---- Stage 5: Atomic reset ----
    await db.execute(
        text("""
            UPDATE dag_nodes
            SET status   = 'pending',
                output_text  = NULL,
                started_at   = NULL,
                completed_at = NULL,
                retry_count  = retry_count + 1,
                assigned_model = COALESCE(:esc_model, assigned_model),
                updated_at   = now()
            WHERE job_id = :jid AND node_key = :nk
        """),
        {"jid": job_id, "nk": node_key, "esc_model": escalation_model},
    )

    if downstream_to_reset:
        await db.execute(
            text("""
                UPDATE dag_nodes
                SET status   = 'pending',
                    output_text  = NULL,
                    started_at   = NULL,
                    completed_at = NULL,
                    updated_at   = now()
                WHERE job_id = :jid AND node_key = ANY(:keys)
            """),
            {"jid": job_id, "keys": downstream_to_reset},
        )

    await db.execute(
        text("""
            UPDATE jobs
            SET status = 'executing',
                compiled_output = NULL,
                updated_at = now()
            WHERE id = :jid AND status IN ('failed', 'blocked')
        """),
        {"jid": job_id},
    )

    await db.commit()

    # ---- Stage 6: Structured log ----
    logger.info(
        "node_retry job_id=%s node_key=%s retry_count=%s downstream_reset=%s",
        job_id, node_key, new_retry_count, len(downstream_to_reset),
    )

    # ---- Stage 7: Return result ----
    return {
        "status": "reset",
        "node_key": node_key,
        "retry_count": new_retry_count,
        "downstream_reset": downstream_to_reset,
    }
