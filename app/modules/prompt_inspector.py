"""
Step 20: Prompt Inspector Module
View and edit optimized prompts for DAG nodes.
"""

import logging
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def list_prompts(job_id: UUID, db: AsyncSession) -> dict:
    """List all prompts for a job's DAG nodes."""
    result = await db.execute(
        text("""
            SELECT node_key, title, status, execution_order,
                   prompt_template, optimized_prompt
            FROM dag_nodes
            WHERE job_id = :job_id
            ORDER BY execution_order
        """),
        {"job_id": str(job_id)}
    )
    rows = result.fetchall()

    if not rows:
        return {"job_id": str(job_id), "node_count": 0, "nodes": []}

    nodes = []
    for r in rows:
        nodes.append({
            "node_key": r.node_key,
            "title": r.title,
            "status": r.status,
            "execution_order": r.execution_order,
            "has_template": bool(r.prompt_template),
            "has_optimized": bool(r.optimized_prompt),
            "template_preview": (r.prompt_template or "")[:120] + ("..." if r.prompt_template and len(r.prompt_template) > 120 else ""),
            "optimized_preview": (r.optimized_prompt or "")[:120] + ("..." if r.optimized_prompt and len(r.optimized_prompt) > 120 else ""),
        })

    return {"job_id": str(job_id), "node_count": len(nodes), "nodes": nodes}


async def get_prompt(job_id: UUID, node_key: str, db: AsyncSession) -> dict:
    """Get full prompt details for a specific node."""
    result = await db.execute(
        text("""
            SELECT node_key, title, status, execution_order,
                   assigned_model, prompt_template, optimized_prompt, output_text
            FROM dag_nodes
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key}
    )
    row = result.fetchone()

    if not row:
        return {"error": f"Node '{node_key}' not found in job {job_id}"}

    return {
        "job_id": str(job_id),
        "node_key": row.node_key,
        "title": row.title,
        "status": row.status,
        "execution_order": row.execution_order,
        "assigned_model": row.assigned_model,
        "prompt_template": row.prompt_template or "",
        "optimized_prompt": row.optimized_prompt or "",
        "has_output": bool(row.output_text),
        "output_preview": (row.output_text or "")[:200] + ("..." if row.output_text and len(row.output_text) > 200 else ""),
    }


async def update_prompt(
    job_id: UUID,
    node_key: str,
    new_prompt: str,
    db: AsyncSession,
    edited_by: str | None = None,
    source: str = "manual",
) -> dict:
    """Update the optimized prompt for a node, recording the previous prompt
    as an immutable revision (audit items #7.8, #7.9).

    Only allowed on pending/failed nodes. Revision numbers are monotonic per
    (job_id, node_key) — the first edit lands as revision 1 and stores the
    ORIGINAL prompt; subsequent edits increment.

    Args:
        edited_by: Optional caller identifier (e.g. user id, "scheduler").
        source: Origin of the edit. One of: manual, optimizer, initial, system.
    """
    if not new_prompt or not new_prompt.strip():
        return {"error": "new_prompt must be a non-empty string"}
    if len(new_prompt) > 16384:
        return {"error": f"new_prompt exceeds 16 KB limit ({len(new_prompt)} bytes)"}
    if source not in ("manual", "optimizer", "initial", "system"):
        return {"error": f"invalid source '{source}'"}

    # 1. Read current state
    result = await db.execute(
        text("""
            SELECT status, optimized_prompt, prompt_template
            FROM dag_nodes
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key}
    )
    row = result.fetchone()

    if not row:
        return {"error": f"Node '{node_key}' not found in job {job_id}"}

    if row.status not in ("pending", "failed"):
        return {"error": f"Cannot edit prompt for node in '{row.status}' state. Only pending/failed nodes can be edited."}

    old_prompt = row.optimized_prompt or row.prompt_template or ""

    # 2. Compute next revision number (atomic via UNIQUE constraint).
    rev_result = await db.execute(
        text("""
            SELECT COALESCE(MAX(revision_number), 0) AS max_rev
            FROM prompt_revisions
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key},
    )
    next_rev = (rev_result.scalar() or 0) + 1

    # 3. Write revision row capturing the OLD prompt before the UPDATE.
    #    Skips if old prompt is empty (no point archiving "").
    if old_prompt:
        await db.execute(
            text("""
                INSERT INTO prompt_revisions
                    (job_id, node_key, revision_number, prompt_text,
                     edited_by, source)
                VALUES
                    (:job_id, :node_key, :rev, :prompt, :edited_by, :source)
            """),
            {
                "job_id": str(job_id),
                "node_key": node_key,
                "rev": next_rev,
                "prompt": old_prompt,
                "edited_by": edited_by,
                "source": source,
            },
        )

    # 4. Apply the new prompt.
    await db.execute(
        text("""
            UPDATE dag_nodes
            SET optimized_prompt = :new_prompt
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key, "new_prompt": new_prompt}
    )
    await db.commit()

    logger.info(
        "prompt_updated: node=%s job=%s rev=%d source=%s",
        node_key, job_id, next_rev, source,
    )

    return {
        "job_id": str(job_id),
        "node_key": node_key,
        "updated": True,
        "revision_number": next_rev if old_prompt else 0,
        "old_length": len(old_prompt),
        "new_length": len(new_prompt),
    }


async def get_history(job_id: UUID, node_key: str, db: AsyncSession) -> dict:
    """Return the full revision history for a node's prompt, newest-first.

    Closes audit items #7.8 (no audit trail) and #7.9 (returns structured
    model instead of flat dict) — the caller wraps this in PromptHistoryResponse.
    """
    # Confirm the node exists and grab the current prompt.
    node_result = await db.execute(
        text("""
            SELECT optimized_prompt, prompt_template
            FROM dag_nodes
            WHERE job_id = :job_id AND node_key = :node_key
        """),
        {"job_id": str(job_id), "node_key": node_key},
    )
    node_row = node_result.fetchone()
    if not node_row:
        return {"error": f"Node '{node_key}' not found in job {job_id}"}

    current_prompt = node_row.optimized_prompt or node_row.prompt_template or ""

    # Pull revisions newest first (already indexed DESC).
    rev_result = await db.execute(
        text("""
            SELECT revision_number, prompt_text, edited_at, edited_by, source
            FROM prompt_revisions
            WHERE job_id = :job_id AND node_key = :node_key
            ORDER BY revision_number DESC
        """),
        {"job_id": str(job_id), "node_key": node_key},
    )
    revisions = [
        {
            "revision_number": r.revision_number,
            "prompt_text": r.prompt_text,
            "edited_at": r.edited_at,
            "edited_by": r.edited_by,
            "source": r.source,
        }
        for r in rev_result.fetchall()
    ]

    return {
        "job_id": str(job_id),
        "node_key": node_key,
        "current_prompt": current_prompt,
        "revision_count": len(revisions),
        "revisions": revisions,
    }
