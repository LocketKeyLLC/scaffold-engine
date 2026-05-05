"""Assistant Mode — human-in-the-loop DAG step walker.

Walks a job's DAG one node at a time, captures human-supplied output
(text, command output, file diffs, etc.) as the node's `output_text`,
and gates downstream nodes on dependency satisfaction the same way the
autonomous executor does.

Authoritative state lives in two tables:
- `assist_sessions`  — one row per active human-driven walk
- `assist_steps`     — one row per (session, node_key)

On commit, the human's evidence is mirrored to `dag_nodes.output_text`
in the same transaction, which is what makes the existing
`_compile_output`, `_fetch_upstream_outputs`, and downstream RAG
grounding paths Just Work without any awareness of assist mode.

See docs/ARCHITECTURE.md "Assistant Mode" + references/assist.md.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any, AsyncGenerator, Optional

from sqlalchemy import text

from app.database import async_session
from app.modules.prompt_assembly import StepContext, assemble_step_context

logger = logging.getLogger("scaffold.assist")


# ── Session lifecycle ────────────────────────────────────────────────────


_VALID_START_STATUSES = (
    "planning", "executing", "blocked", "failed",
    # Allow re-entry from an existing assist status — start_assist_session
    # is idempotent on (job_id) via the UNIQUE constraint.
    "assisted_executing", "assisted_running", "assisted_paused",
)


async def start_assist_session(
    *,
    job_id: str,
    handoff_policy: str = "manual",
    replan_policy: str = "context_only",
    db,
) -> dict:
    """Promote a job into assist mode.

    Idempotent on job_id (UNIQUE in the schema). If a session already
    exists, return its id without re-seeding steps.

    Side effects (all in one transaction):
      - INSERT assist_sessions row (or no-op if it exists)
      - UPDATE jobs.status -> 'assisted_executing'
      - INSERT one assist_steps row per `dag_nodes` row in pending status
    """
    # 1. Validate job state.
    row = (await db.execute(
        text("SELECT id, status FROM jobs WHERE id = :id"),
        {"id": job_id},
    )).mappings().first()
    if not row:
        raise ValueError(f"job not found: {job_id}")
    if row["status"] not in _VALID_START_STATUSES:
        raise ValueError(
            f"job {job_id} is in status {row['status']!r}; "
            f"assist mode requires one of {_VALID_START_STATUSES}"
        )

    # 2. Idempotent insert. ON CONFLICT collapses concurrent starts.
    sess_row = (await db.execute(
        text("""
            INSERT INTO assist_sessions
                (job_id, handoff_policy, replan_policy, status)
            VALUES (:jid, :hp, :rp, 'active')
            ON CONFLICT (job_id) DO UPDATE
                SET last_activity_at = NOW()
            RETURNING id, job_id, status, handoff_policy, replan_policy
        """),
        {"jid": job_id, "hp": handoff_policy, "rp": replan_policy},
    )).mappings().first()
    session_id = str(sess_row["id"])

    # 3. Job status transition (idempotent).
    await db.execute(
        text("""
            UPDATE jobs SET status = 'assisted_executing', updated_at = NOW()
             WHERE id = :id
               AND status IN ('planning', 'executing', 'blocked', 'failed')
        """),
        {"id": job_id},
    )

    # 4. Seed assist_steps from dag_nodes (idempotent via UNIQUE).
    await db.execute(
        text("""
            INSERT INTO assist_steps (session_id, job_id, node_key, status)
            SELECT :sid, :jid, node_key, 'pending'
              FROM dag_nodes
             WHERE job_id = :jid AND status NOT IN ('done', 'skipped')
            ON CONFLICT (session_id, node_key) DO NOTHING
        """),
        {"sid": session_id, "jid": job_id},
    )

    total = (await db.execute(
        text("SELECT COUNT(*) FROM assist_steps WHERE session_id = :sid"),
        {"sid": session_id},
    )).scalar()
    pending = (await db.execute(
        text("""
            SELECT COUNT(*) FROM assist_steps
             WHERE session_id = :sid AND status = 'pending'
        """),
        {"sid": session_id},
    )).scalar()

    await db.commit()
    logger.info(
        "assist_session_started session_id=%s job_id=%s total_steps=%d pending=%d",
        session_id, job_id, total, pending,
    )
    return {
        "session_id": session_id,
        "job_id": job_id,
        "status": sess_row["status"],
        "handoff_policy": sess_row["handoff_policy"],
        "replan_policy": sess_row["replan_policy"],
        "total_steps": total,
        "pending_steps": pending,
    }


async def get_session(*, session_id: str, db) -> Optional[dict]:
    """Return session + step roll-up. None if not found."""
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, handoff_policy,
                   replan_policy, started_at, last_activity_at, completed_at
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        return None
    rollup = (await db.execute(
        text("""
            SELECT status, COUNT(*) AS cnt FROM assist_steps
             WHERE session_id = :sid GROUP BY status
        """),
        {"sid": session_id},
    )).mappings().all()
    return {
        **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(sess).items()},
        "step_counts": {r["status"]: r["cnt"] for r in rollup},
    }


# ── Step retrieval ───────────────────────────────────────────────────────


async def get_next_step(*, session_id: str, db) -> Optional[dict]:
    """Atomically claim the next pending step whose deps are satisfied.

    Returns the step + assembled context, or None when the session is
    complete (no pending steps with satisfied deps remain). The caller
    should treat None as "session complete" only when also no
    presented/awaiting_input/received/applied steps are in flight —
    otherwise it just means the user has work to submit.

    Concurrency: an atomic UPDATE with FOR UPDATE SKIP LOCKED prevents
    two readers from claiming the same step.
    """
    # Validate session is active.
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        return None
    if sess["status"] != "active":
        return None
    job_id = str(sess["job_id"])

    # Claim atomically. Dep gating: every node listed in dag_nodes.depends_on
    # must be 'done' or 'skipped' on dag_nodes.
    claimed = (await db.execute(
        text("""
            UPDATE assist_steps
               SET status = 'presented', presented_at = NOW(), updated_at = NOW()
             WHERE id = (
                SELECT s.id FROM assist_steps s
                JOIN dag_nodes d
                  ON d.job_id = s.job_id AND d.node_key = s.node_key
                WHERE s.session_id = :sid
                  AND s.status = 'pending'
                  AND NOT EXISTS (
                      SELECT 1 FROM unnest(d.depends_on) dep_key
                      WHERE NOT EXISTS (
                          SELECT 1 FROM dag_nodes ud
                           WHERE ud.job_id = s.job_id
                             AND ud.node_key = dep_key
                             AND ud.status IN ('done', 'skipped')
                      )
                  )
                ORDER BY d.execution_order NULLS LAST, s.node_key
                FOR UPDATE OF s SKIP LOCKED
                LIMIT 1
             )
             AND status = 'pending'
            RETURNING id, node_key
        """),
        {"sid": session_id},
    )).mappings().first()
    if not claimed:
        await db.commit()
        return None

    node_key = claimed["node_key"]
    await db.execute(
        text("""
            UPDATE assist_sessions
               SET current_node_key = :nk, last_activity_at = NOW(), updated_at = NOW(),
                   status = CASE WHEN status = 'active' THEN 'active' ELSE status END
             WHERE id = :sid
        """),
        {"sid": session_id, "nk": node_key},
    )
    await db.execute(
        text("UPDATE jobs SET status = 'assisted_running', updated_at = NOW() "
             "WHERE id = :jid AND status = 'assisted_executing'"),
        {"jid": job_id},
    )
    await db.commit()

    # Build the human-facing context (no grounding fetch by default; the
    # human already has the knowledge — pre-fetching just adds noise
    # unless explicitly requested via a future include_grounding flag).
    node_row = (await db.execute(
        text("""
            SELECT node_key, title, description, prompt_template, depends_on,
                   tool, domain, execution_order
              FROM dag_nodes
             WHERE job_id = :jid AND node_key = :nk
        """),
        {"jid": job_id, "nk": node_key},
    )).mappings().first()
    job_row = (await db.execute(
        text("SELECT refined_brief FROM jobs WHERE id = :id"),
        {"id": job_id},
    )).mappings().first()
    brief = (job_row or {}).get("refined_brief") or {}

    ctx: StepContext = await assemble_step_context(
        db=db,
        job_id=job_id,
        node=dict(node_row),
        brief=brief,
        fetch_grounding=None,
    )

    return {
        "session_id": session_id,
        "job_id": job_id,
        "node_key": ctx.node_key,
        "title": ctx.title,
        "description": node_row.get("description"),
        "tool": ctx.tool,
        "domain": ctx.domain,
        "depends_on": list(node_row.get("depends_on") or []),
        "system_prompt": ctx.system_prompt,
        "base_prompt": ctx.base_prompt,
        "upstream_outputs": ctx.upstream_outputs,
        "upstream_truncated_keys": ctx.upstream_truncated_keys,
        "assembled_prompt": ctx.assembled_prompt,
    }


# ── Submit / commit human evidence ───────────────────────────────────────


async def submit_step(
    *,
    session_id: str,
    node_key: str,
    evidence: str,
    evidence_kind: str = "text",
    evidence_meta: dict | None = None,
    action: str = "submit",
    friction_note: str | None = None,
    db,
) -> dict:
    """Record human evidence for one step. Mirrors to `dag_nodes.output_text`.

    `action` is "submit" (mark dag_node done) or "skip" (mark skipped).

    Concurrency: requires the step's prior status to be presented /
    awaiting_input / received. Double-submit is a no-op.
    """
    if action not in ("submit", "skip"):
        raise ValueError(f"action must be 'submit' or 'skip', got {action!r}")
    if action == "submit" and not evidence:
        raise ValueError("submit requires non-empty evidence")
    evidence_meta = dict(evidence_meta or {})
    evidence_meta.setdefault("by", "human")
    if evidence_kind:
        evidence_meta.setdefault("evidence_kind", evidence_kind)
    meta_json = json.dumps(evidence_meta)

    # Validate session active + step claim-ready.
    step = (await db.execute(
        text("""
            SELECT s.id AS step_id, s.status, s.session_id, s.job_id, s.node_key
              FROM assist_steps s
              JOIN assist_sessions ss ON ss.id = s.session_id
             WHERE s.session_id = :sid AND s.node_key = :nk
               AND ss.status IN ('active', 'paused')
             FOR UPDATE OF s
        """),
        {"sid": session_id, "nk": node_key},
    )).mappings().first()
    if not step:
        raise ValueError(f"step not found or session not active: {session_id}/{node_key}")
    if step["status"] not in ("presented", "awaiting_input", "received"):
        # Idempotent: already-committed submits return current state, not error.
        if step["status"] in ("committed", "skipped"):
            await db.commit()
            return {
                "session_id": session_id,
                "node_key": node_key,
                "status": step["status"],
                "no_op": True,
            }
        raise ValueError(
            f"step {node_key} status {step['status']!r} cannot accept submit"
        )

    job_id = str(step["job_id"])
    if action == "skip":
        await db.execute(
            text("""
                UPDATE assist_steps
                   SET status = 'skipped',
                       submitted_at = NOW(), committed_at = NOW(),
                       evidence_kind = 'none',
                       evidence_meta = evidence_meta || CAST(:meta AS jsonb),
                       friction_note = COALESCE(:fn, friction_note),
                       updated_at = NOW()
                 WHERE id = :id
            """),
            {"id": step["step_id"], "meta": meta_json, "fn": friction_note},
        )
        await db.execute(
            text("""
                UPDATE dag_nodes
                   SET status = 'skipped', updated_at = NOW(), completed_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk
                   AND status NOT IN ('done', 'skipped')
            """),
            {"jid": job_id, "nk": node_key},
        )
        committed_status = "skipped"
    else:
        # Mirror to dag_nodes.output_text + flip to 'done'. Same transaction.
        await db.execute(
            text("""
                UPDATE assist_steps
                   SET status = 'committed',
                       submitted_at = NOW(), committed_at = NOW(),
                       evidence = :ev, evidence_kind = :ek,
                       evidence_meta = evidence_meta || CAST(:meta AS jsonb),
                       friction_note = COALESCE(:fn, friction_note),
                       updated_at = NOW()
                 WHERE id = :id
            """),
            {
                "id": step["step_id"],
                "ev": evidence,
                "ek": evidence_kind,
                "meta": meta_json,
                "fn": friction_note,
            },
        )
        await db.execute(
            text("""
                UPDATE dag_nodes
                   SET output_text = :out,
                       status = 'done',
                       updated_at = NOW(),
                       completed_at = NOW()
                 WHERE job_id = :jid AND node_key = :nk
                   AND status NOT IN ('done', 'skipped')
            """),
            {"jid": job_id, "nk": node_key, "out": evidence},
        )
        committed_status = "committed"

    await db.execute(
        text("""
            UPDATE assist_sessions
               SET last_activity_at = NOW(), updated_at = NOW()
             WHERE id = :sid
        """),
        {"sid": session_id},
    )
    await db.commit()
    logger.info(
        "assist_step_%s session_id=%s node_key=%s evidence_kind=%s chars=%d",
        committed_status, session_id, node_key, evidence_kind,
        len(evidence) if evidence else 0,
    )
    # Re-plan check (only on action='submit'; skip evidence is not divergence).
    replan_result = None
    if action == "submit":
        replan_result = await _maybe_replan(
            session_id=session_id, job_id=job_id,
            node_key=node_key, evidence=evidence, db=db,
        )
    # Detect session completion.
    next_pending = await _next_pending_node_key(session_id=session_id, db=db)
    if next_pending is None:
        await _maybe_finalize_session(session_id=session_id, db=db)
    return {
        "session_id": session_id,
        "node_key": node_key,
        "status": committed_status,
        "no_op": False,
        "next_node_key": next_pending,
        "replan": replan_result,
    }


async def _maybe_replan(
    *, session_id: str, job_id: str, node_key: str, evidence: str, db,
) -> dict | None:
    """Pull session/node metadata and dispatch to assist_replan.maybe_replan."""
    sess = (await db.execute(
        text("SELECT replan_policy FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        return None
    policy = sess["replan_policy"]
    if policy == "disabled":
        return None
    node = (await db.execute(
        text("""
            SELECT title, prompt_template
              FROM dag_nodes WHERE job_id = :jid AND node_key = :nk
        """),
        {"jid": job_id, "nk": node_key},
    )).mappings().first()
    if not node:
        return None
    from app.modules import assist_replan
    return await assist_replan.maybe_replan(
        db=db,
        session_id=session_id,
        job_id=job_id,
        node_key=node_key,
        title=node["title"],
        prompt=node["prompt_template"] or "",
        evidence=evidence,
        policy=policy,
    )


async def _next_pending_node_key(*, session_id: str, db) -> Optional[str]:
    row = (await db.execute(
        text("""
            SELECT node_key FROM assist_steps
             WHERE session_id = :sid
               AND status IN ('pending', 'presented', 'awaiting_input', 'received')
             ORDER BY node_key LIMIT 1
        """),
        {"sid": session_id},
    )).mappings().first()
    return row["node_key"] if row else None


async def _maybe_finalize_session(*, session_id: str, db) -> None:
    """If all steps are terminal, mark session + job completed."""
    incomplete = (await db.execute(
        text("""
            SELECT COUNT(*) FROM assist_steps
             WHERE session_id = :sid
               AND status NOT IN ('committed', 'skipped', 'handed_off', 'escalated')
        """),
        {"sid": session_id},
    )).scalar()
    if incomplete:
        return
    sess = (await db.execute(
        text("""
            UPDATE assist_sessions
               SET status = 'completed', completed_at = NOW(), updated_at = NOW()
             WHERE id = :sid AND status IN ('active', 'paused')
            RETURNING job_id
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        return
    await db.execute(
        text("""
            UPDATE jobs
               SET status = 'completed', completed_at = NOW(), updated_at = NOW()
             WHERE id = :jid
               AND status IN ('assisted_executing', 'assisted_running', 'assisted_paused')
        """),
        {"jid": sess["job_id"]},
    )
    await db.commit()
    logger.info("assist_session_completed session_id=%s job_id=%s",
                session_id, sess["job_id"])


# ── Pause / resume / abandon ─────────────────────────────────────────────


async def pause_session(*, session_id: str, db) -> dict:
    sess = (await db.execute(
        text("""
            UPDATE assist_sessions SET status = 'paused', updated_at = NOW()
             WHERE id = :sid AND status = 'active'
            RETURNING id, job_id
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"session not active: {session_id}")
    await db.execute(
        text("UPDATE jobs SET status = 'assisted_paused', updated_at = NOW() "
             "WHERE id = :jid AND status IN ('assisted_executing', 'assisted_running')"),
        {"jid": sess["job_id"]},
    )
    await db.commit()
    return {"session_id": session_id, "status": "paused"}


async def resume_session(*, session_id: str, db) -> dict:
    sess = (await db.execute(
        text("""
            UPDATE assist_sessions SET status = 'active', updated_at = NOW(),
                   last_activity_at = NOW()
             WHERE id = :sid AND status = 'paused'
            RETURNING id, job_id
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"session not paused: {session_id}")
    await db.execute(
        text("UPDATE jobs SET status = 'assisted_executing', updated_at = NOW() "
             "WHERE id = :jid AND status = 'assisted_paused'"),
        {"jid": sess["job_id"]},
    )
    await db.commit()
    return {"session_id": session_id, "status": "active"}


async def abandon_session(*, session_id: str, db) -> dict:
    sess = (await db.execute(
        text("""
            UPDATE assist_sessions SET status = 'abandoned',
                   completed_at = NOW(), updated_at = NOW()
             WHERE id = :sid AND status IN ('active', 'paused')
            RETURNING id, job_id
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"session not active or paused: {session_id}")
    await db.execute(
        text("""
            UPDATE jobs SET status = 'cancelled',
                   error_summary = COALESCE(error_summary, 'Assist session abandoned'),
                   updated_at = NOW()
             WHERE id = :jid
               AND status IN ('assisted_executing', 'assisted_running', 'assisted_paused')
        """),
        {"jid": sess["job_id"]},
    )
    await db.commit()
    return {"session_id": session_id, "status": "abandoned"}


# ── Friction log ─────────────────────────────────────────────────────────


async def record_friction(
    *, session_id: str, node_key: str, note: str, db
) -> None:
    """Append a friction note to a step. Idempotent text concat with timestamp."""
    if not note:
        return
    await db.execute(
        text("""
            UPDATE assist_steps
               SET friction_note = COALESCE(friction_note || E'\n', '')
                                 || to_char(NOW(), 'YYYY-MM-DD HH24:MI ')
                                 || :note,
                   updated_at = NOW()
             WHERE session_id = :sid AND node_key = :nk
        """),
        {"sid": session_id, "nk": node_key, "note": note},
    )
    await db.commit()


async def list_friction(*, session_id: str, db) -> list[dict]:
    rows = (await db.execute(
        text("""
            SELECT node_key, friction_note, status, committed_at
              FROM assist_steps
             WHERE session_id = :sid AND friction_note IS NOT NULL
             ORDER BY node_key
        """),
        {"sid": session_id},
    )).mappings().all()
    return [dict(r) for r in rows]


# ── Handoff (assist -> autonomous executor for one node or all remaining) ─


async def handoff_step(
    *, session_id: str, node_key: str, mode: str, db
) -> AsyncGenerator[str, None]:
    """Hand a node back to the autonomous executor.

    `mode` is 'single' (one node, then back to assist) or 'all_remaining'
    (autonomous takes the rest of the DAG).

    Yields SSE-formatted strings from the underlying executor.
    """
    if mode not in ("single", "all_remaining"):
        raise ValueError(f"mode must be 'single' or 'all_remaining', got {mode!r}")
    sess = (await db.execute(
        text("""
            SELECT id, job_id FROM assist_sessions
             WHERE id = :sid AND status = 'active'
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"session not active: {session_id}")
    job_id = str(sess["job_id"])

    # Mark step(s) as handed_off so assist won't re-claim them.
    if mode == "single":
        await db.execute(
            text("""
                UPDATE assist_steps SET status = 'handed_off', updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :nk
                   AND status IN ('pending', 'presented', 'awaiting_input')
            """),
            {"sid": session_id, "nk": node_key},
        )
    else:
        await db.execute(
            text("""
                UPDATE assist_steps SET status = 'handed_off', updated_at = NOW()
                 WHERE session_id = :sid
                   AND status IN ('pending', 'presented', 'awaiting_input')
            """),
            {"sid": session_id},
        )
    await db.commit()

    # Switch the job out of assisted_* into 'executing' so the autonomous
    # executor's status whitelist accepts it. We flip back to assist on
    # completion (single mode) or leave it in autonomous (all_remaining).
    async with async_session() as db2:
        await db2.execute(
            text("UPDATE jobs SET status = 'executing', updated_at = NOW() "
                 "WHERE id = :jid"),
            {"jid": job_id},
        )
        await db2.commit()

    yield _sse("assist_handoff_started", {
        "session_id": session_id,
        "node_key": node_key,
        "mode": mode,
    })

    # Defer import to avoid a heavy module-level dep on execution_agent.
    from app.modules.execution_agent import execute_all_nodes

    try:
        async for ev in execute_all_nodes(job_id):
            yield ev
    finally:
        # On return, restore assist mode unless all_remaining took over.
        if mode == "single":
            async with async_session() as db3:
                # Only restore if the session is still active.
                still = (await db3.execute(
                    text("SELECT status FROM assist_sessions WHERE id = :sid"),
                    {"sid": session_id},
                )).scalar()
                if still == "active":
                    await db3.execute(
                        text("UPDATE jobs SET status = 'assisted_executing', updated_at = NOW() "
                             "WHERE id = :jid AND status NOT IN ('completed', 'failed', 'cancelled')"),
                        {"jid": job_id},
                    )
                    await db3.commit()

    yield _sse("assist_handoff_done", {
        "session_id": session_id,
        "node_key": node_key,
        "mode": mode,
    })


def _sse(event_type: str, payload: dict) -> str:
    """SSE wire format. Same shape as research_agent / execution_agent."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
