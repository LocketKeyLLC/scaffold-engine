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

See OVERVIEW.md §9 ("Assist Mode") for the design.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid as _uuid
from dataclasses import asdict
from typing import Any, AsyncGenerator, Optional

from sqlalchemy import text

from app.database import async_session
from app.modules.prompt_assembly import (
    StepContext,
    assemble_job_digest,
    assemble_step_context,
)

logger = logging.getLogger("scaffold.assist")


# ── Session lifecycle ────────────────────────────────────────────────────


_VALID_START_STATUSES = (
    "planning", "executing", "blocked", "failed",
    # §17.624 — the hands-on assist gate parks a predominantly-Shell/human job
    # here specifically so the operator drives it via /assist; its nodes are
    # already 'pending', so start seeds steps directly (no re-open reset needed).
    "awaiting_assist",
    # Allow re-entry from an existing assist status — start_assist_session
    # is idempotent on (job_id) via the UNIQUE constraint.
    "assisted_executing", "assisted_running", "assisted_paused",
)

# §17.623 — a job that already ran to a terminal state can be RE-OPENED into
# assist mode for a hands-on redo. The trigger case: a hardware/infra job (e.g.
# a home-lab build) that decomposed and ran autonomously, fabricating per-node
# "done" evidence, then refused `/assist` with a confusing "already completed"
# 409. Re-open resets every DAG node to pending so the assist session seeds a
# full walkthrough; the job-level compiled_output is left as the archive and is
# regenerated when the assist session finalizes.
_TERMINAL_REOPEN_STATUSES = ("completed", "cancelled")


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

    Umbrella jobs and any job with 0 DAG nodes return early with
    ``{"assist_unavailable": True, ...}`` and NO side effects (§17.561).

    A job in a terminal ``completed``/``cancelled`` status is RE-OPENED
    (§17.623): its DAG nodes are reset to pending, prior per-node output is
    cleared, and the job returns to ``assisted_executing`` for a hands-on redo.
    The return dict carries ``reopened: True`` in that case.

    Side effects (all in one transaction) for assistable jobs:
      - INSERT assist_sessions row (or no-op if it exists)
      - (re-open only) reset dag_nodes -> pending, clearing output_text
      - UPDATE jobs.status -> 'assisted_executing'
      - INSERT one assist_steps row per `dag_nodes` row in pending status
      - (re-open only) reset any pre-existing assist_steps -> pending
    """
    # §17.521 — validate job_id is a UUID BEFORE it reaches the query. A
    # non-UUID (e.g. a pasted job TITLE like "DeFruscio HomeLab") otherwise
    # hits asyncpg's uuid cast and surfaces as a raw HTTP 500 DBAPIError
    # ("invalid input for query argument $1 … invalid UUID"). Raise a clean
    # ValueError → the endpoint maps it to a friendly 4xx with a helpful hint.
    try:
        _uuid.UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            f"invalid job_id {job_id!r}: not a job id (expected a UUID). "
            f"Job titles aren't accepted — find the id with /jobs."
        )

    # 1. Validate job state.
    row = (await db.execute(
        text("""
            SELECT j.id, j.status, j.job_type,
                   (SELECT COUNT(*) FROM dag_nodes WHERE job_id = j.id)
                       AS node_count
              FROM jobs j WHERE j.id = :id
        """),
        {"id": job_id},
    )).mappings().first()
    if not row:
        raise ValueError(f"job not found: {job_id}")

    # §17.561 — umbrella / 0-node guard. Umbrella jobs (multi-part
    # decompositions) intentionally have NO DAG nodes of their own — the work
    # runs in autonomous component children. Before the gate, starting assist
    # on one seeded an empty session and `/assist next` rendered the cryptic
    # "⏳ No step ready right now." Detect it BEFORE creating any session row
    # and return structured guidance the router surfaces as a friendly 200
    # ("components run automatically; watch with /results"), not an error.
    # Runs ahead of the status check so an 'aggregating' umbrella also gets the
    # guidance rather than a confusing 409.
    if row["job_type"] == "umbrella" or row["node_count"] == 0:
        children: list[dict] = []
        if row["job_type"] == "umbrella":
            child_rows = (await db.execute(
                text("""
                    SELECT id, title, status, component_index
                      FROM jobs WHERE parent_job_id = :u
                     ORDER BY component_index
                """),
                {"u": job_id},
            )).mappings().all()
            children = [{
                "job_id": str(c["id"]),
                "title": c["title"],
                "status": c["status"],
                "component_index": c["component_index"],
            } for c in child_rows]
        reason = "umbrella" if row["job_type"] == "umbrella" else "no_dag"
        logger.info(
            "assist_unavailable job_id=%s reason=%s job_type=%s nodes=%d",
            job_id, reason, row["job_type"], row["node_count"],
        )
        return {
            "assist_unavailable": True,
            "reason": reason,
            "job_id": job_id,
            "job_type": row["job_type"],
            "job_status": row["status"],
            "children": children,
            "children_total": len(children),
        }

    reopening = row["status"] in _TERMINAL_REOPEN_STATUSES
    # §17.682 — reconnecting to a PAUSED job (job='assisted_paused', session
    # 'paused') is a RESUME, not a redo: reactivate in place, never reset work.
    resuming_paused = row["status"] == "assisted_paused"
    if not reopening and row["status"] not in _VALID_START_STATUSES:
        raise ValueError(
            f"job {job_id} is in status {row['status']!r}; "
            f"assist mode requires one of "
            f"{_VALID_START_STATUSES + _TERMINAL_REOPEN_STATUSES}"
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

    # §17.623 — re-open reset. The job already ran to a terminal state; reset
    # every non-pending DAG node so the assist session seeds a full redo. Clear
    # the fabricated per-node output_text so it can't pollute the assist
    # upstream-context (the job-level compiled_output stays as the archive).
    if reopening:
        await db.execute(
            text("""
                UPDATE dag_nodes
                   SET status = 'pending', output_text = NULL,
                       started_at = NULL, completed_at = NULL,
                       retry_count = 0, updated_at = NOW()
                 WHERE job_id = :jid AND status <> 'pending'
            """),
            {"jid": job_id},
        )

    # 3. Job status transition (idempotent). Includes the re-open statuses
    # (completed/cancelled → §17.623) AND awaiting_assist (§17.624 hands-on
    # gate) so the job moves into assisted_executing; without awaiting_assist
    # here the job stays parked through the whole walkthrough and
    # _maybe_finalize_session (WHERE status IN assisted_*) can never mark it
    # 'completed'. §17.682 — assisted_paused too, so reconnecting a paused job
    # resumes it (mirrors resume_session's job flip). completed_at is cleared
    # (harmless no-op for the non-terminal start paths).
    await db.execute(
        text("""
            UPDATE jobs
               SET status = 'assisted_executing', completed_at = NULL,
                   updated_at = NOW()
             WHERE id = :id
               AND status IN ('planning', 'executing', 'blocked', 'failed',
                              'completed', 'cancelled', 'awaiting_assist',
                              'assisted_paused')
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

    # §17.623 — re-open of a job that had a PRIOR assist session (session row
    # reused via ON CONFLICT): the INSERT above no-ops on existing (session,
    # node) rows, so reset any non-pending step back to pending for a clean redo.
    if reopening:
        await db.execute(
            text("""
                UPDATE assist_steps
                   SET status = 'pending', evidence = NULL, evidence_kind = NULL,
                       presented_at = NULL, submitted_at = NULL,
                       committed_at = NULL, divergence = FALSE,
                       replan_triggered = FALSE, updated_at = NOW()
                 WHERE session_id = :sid AND status <> 'pending'
            """),
            {"sid": session_id},
        )
        # §17.681 — the ON CONFLICT above only bumps last_activity_at; it does
        # NOT touch status. A prior session that finished (completed) or was
        # reaped (abandoned/cancelled) therefore stayed terminal, so the reopened
        # session yielded NO steps: get_next_step bails on status != 'active' and
        # _maybe_finalize_session only finalizes active|paused. Force it live so
        # the deliberate hands-on redo actually works.
        await db.execute(
            text("""
                UPDATE assist_sessions
                   SET status = 'active', completed_at = NULL, updated_at = NOW()
                 WHERE id = :sid AND status <> 'active'
            """),
            {"sid": session_id},
        )

    # §17.682 — reconnecting to a PAUSED job resumes it. The ON CONFLICT above
    # left the reused session 'paused' and the job flip above moved the job back
    # to assisted_executing; mirror resume_session on the session row so
    # get_next_step (which requires status='active') hands out the next step.
    # Unlike the reopen path this must NOT reset nodes/steps — paused work
    # continues exactly where the operator left off.
    if resuming_paused:
        await db.execute(
            text("""
                UPDATE assist_sessions
                   SET status = 'active', last_activity_at = NOW(), updated_at = NOW()
                 WHERE id = :sid AND status = 'paused'
            """),
            {"sid": session_id},
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
        "assist_session_started session_id=%s job_id=%s total_steps=%d "
        "pending=%d reopened=%s prev_status=%s",
        session_id, job_id, total, pending, reopening, row["status"],
    )
    return {
        "session_id": session_id,
        "job_id": job_id,
        # §17.681/682 — on reopen (terminal) or resume (paused) we forced the
        # session back to 'active' above; sess_row was RETURNED from the pre-reset
        # ON CONFLICT, so report the live status, not the stale one.
        "status": "active" if (reopening or resuming_paused) else sess_row["status"],
        "handoff_policy": sess_row["handoff_policy"],
        "replan_policy": sess_row["replan_policy"],
        "total_steps": total,
        "pending_steps": pending,
        # §17.623 — True when this start re-opened a terminal (completed/
        # cancelled) job for a hands-on redo. The pipeline surfaces a banner so
        # the operator knows the prior autonomous output was archived.
        "reopened": reopening,
    }


async def get_session(*, session_id: str, db) -> Optional[dict]:
    """Return session + step roll-up. None if not found."""
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, handoff_policy,
                   replan_policy, started_at, last_activity_at, completed_at, notes
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
    # §17.617 (audit #13) — surface the divergence flag maybe_replan writes. The
    # context_only DEFAULT replan_policy fire-and-forgets a verifier LLM call per
    # submit that sets assist_steps.divergence=TRUE on major divergence, but NO
    # production read consumed it — the run paid for a write-only flag. Now the
    # session roll-up reports how many steps diverged so an operator can see it.
    divergence_count = (await db.execute(
        text("""
            SELECT COUNT(*) FROM assist_steps
             WHERE session_id = :sid AND divergence = TRUE
        """),
        {"sid": session_id},
    )).scalar() or 0
    return {
        **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(sess).items()},
        "step_counts": {r["status"]: r["cnt"] for r in rollup},
        "divergence_count": int(divergence_count),
    }


# ── Step retrieval ───────────────────────────────────────────────────────


async def _load_presented_step(*, session_id: str, job_id: str, db) -> Optional[dict]:
    """Assemble the (earliest) in-flight presented-but-unsubmitted step for this
    session, or None. Shared by the §17.645 one-in-flight guard and the §17.512
    nothing-else-claimable fallback."""
    presented = (await db.execute(
        text("""
            SELECT s.node_key, s.guidance_status
              FROM assist_steps s
              JOIN dag_nodes d
                ON d.job_id = s.job_id AND d.node_key = s.node_key
             WHERE s.session_id = :sid AND s.status = 'presented'
             ORDER BY d.execution_order NULLS LAST, s.node_key
             LIMIT 1
        """),
        {"sid": session_id},
    )).mappings().first()
    if not presented:
        return None
    node_row, ctx = await _assemble_ctx_for_node(
        db=db, job_id=job_id, node_key=presented["node_key"],
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
        "guidance_status": presented.get("guidance_status") or "none",
        "re_presented": True,
    }


async def get_next_step(*, session_id: str, db) -> Optional[dict]:
    """Claim the next step for the human to work.

    §17.645 — ONE step in flight at a time. If a step is already presented but
    not yet submitted/skipped, re-present THAT rather than claiming a new one.
    This keeps the human walkthrough linear — finish or skip the current step
    before the next is handed out — so `next` can't jump to an unrelated
    dependency-ready branch (e.g. a standalone doc/summary node) and then bounce
    back to the unfinished step. Only when nothing is in flight does it claim the
    next pending step whose deps are satisfied.

    Returns the step + assembled context, or None when the session is
    complete (no pending steps with satisfied deps remain and nothing is in
    flight).

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

    # §17.645 — one step in flight at a time. Re-present an already-presented,
    # not-yet-submitted step instead of claiming a new (possibly far) node.
    in_flight = await _load_presented_step(session_id=session_id, job_id=job_id, db=db)
    if in_flight is not None:
        await db.commit()
        return in_flight

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
            RETURNING id, node_key, guidance_status
        """),
        {"sid": session_id},
    )).mappings().first()
    if not claimed:
        await db.commit()
        # §17.512 — no pending step is claimable. Before reporting "nothing to
        # do", re-surface a step already PRESENTED to this user but not yet
        # submitted, so a lost / scrolled-away / reconnect walkthrough is
        # recoverable via `/assist next`. (Post-§17.645 this is normally caught
        # by the in-flight guard above; kept as a belt-and-suspenders fallback
        # for legacy sessions with a presented step but a race on the claim.)
        return await _load_presented_step(session_id=session_id, job_id=job_id, db=db)

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
    node_row, ctx = await _assemble_ctx_for_node(
        db=db, job_id=job_id, node_key=node_key,
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
        # §17.486 — cache state so the client knows whether a cached
        # walkthrough already exists or one will be generated on demand.
        "guidance_status": claimed.get("guidance_status") or "none",
    }


async def _assemble_ctx_for_node(
    *, db, job_id: str, node_key: str,
) -> tuple[dict, "StepContext"]:
    """Fetch a node + brief and assemble the upstream-last StepContext.

    Shared by ``get_next_step`` (claim path) and ``generate_step_guidance``
    (guidance path) so the two cannot drift in what they consider "the step".
    Returns ``(node_row_dict, ctx)``. ``fetch_grounding=None`` — the human's
    walkthrough is grounded by the assist_guide research pre-pass, not here.
    """
    node_row = (await db.execute(
        text("""
            SELECT node_key, title, description, prompt_template, depends_on,
                   tool, domain, execution_order, node_type
              FROM dag_nodes
             WHERE job_id = :jid AND node_key = :nk
        """),
        {"jid": job_id, "nk": node_key},
    )).mappings().first()
    if not node_row:
        raise ValueError(f"node not found: {job_id}/{node_key}")
    job_row = (await db.execute(
        text("SELECT refined_brief FROM jobs WHERE id = :id"),
        {"id": job_id},
    )).mappings().first()
    brief = (job_row or {}).get("refined_brief") or {}

    ctx = await assemble_step_context(
        db=db,
        job_id=job_id,
        node=dict(node_row),
        brief=brief,
        fetch_grounding=None,
    )
    return dict(node_row), ctx


# ── Guidance generation (§17.486 — human walkthrough per step) ────────────


async def generate_step_guidance(
    *,
    session_id: str,
    node_key: str | None = None,
    refine: str | None = None,
    research: bool | None = None,
    force: bool = False,
    db,
) -> dict:
    """Generate (or return cached) the human walkthrough for a step.

    Resolves ``node_key`` from the session's ``current_node_key`` when omitted.
    Delegates generation + caching to ``assist_guide.ensure_guidance``. The
    walkthrough is human-executable instructions (copy-paste commands for
    shell/codegen work, numbered steps for non-coding work), optionally
    grounded by a research pre-pass.
    """
    from app.config import settings
    from app.modules import assist_guide

    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, metadata, notes
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise ValueError(f"session status {sess['status']!r} cannot generate guidance")
    job_id = str(sess["job_id"])
    # §17.639 — resolve through the anti-echo guard: an explicit node_key is
    # honored, but an auto-resolved pointer never targets a *finished* step (it
    # self-heals forward instead of re-rendering the committed/handed-off one).
    nk = await _resolve_live_node_key(
        session_id=session_id, node_key=node_key,
        current_node_key=sess["current_node_key"], db=db,
    )
    if not nk:
        raise ValueError(
            "no live step to guide; the session's steps are all finished — "
            "run /assist done to see the result, or /assist next to re-check"
        )

    if research is None:
        research = settings.assist_guide_research

    environment = _environment_from_metadata(sess.get("metadata"))
    verbosity = _verbosity_from_metadata(sess.get("metadata"))
    node_row, ctx = await _assemble_ctx_for_node(db=db, job_id=job_id, node_key=nk)
    # §17.650 — the whole-project digest (minus this step's direct parents,
    # already in ctx.assembled_prompt) so the walkthrough is consistent with
    # research/plan done in other DAG branches, not just the immediate upstream.
    job_digest = await _job_digest_for(
        db=db, job_id=job_id,
        exclude_node_keys={nk, *ctx.upstream_outputs.keys()},
    )
    # §17.654 — decision nodes get the one-choice-at-a-time, suggest-don't-decide
    # prompt; every step also carries the operator's captured notes & additions
    # (read from the session row already fetched — no extra round-trip).
    is_decision = (node_row.get("node_type") or "").lower() == "decision"
    operator_notes = _coerce_notes(sess.get("notes"))

    res = await assist_guide.ensure_guidance(
        session_id=session_id,
        node_key=nk,
        ctx=ctx,
        node_description=node_row.get("description"),
        research=research,
        refine_hint=refine,
        force=force,
        domain=node_row.get("domain"),
        environment=environment,
        verbosity=verbosity,
        job_digest=job_digest,
        operator_notes=operator_notes,
        is_decision=is_decision,
        db=db,
    )
    return {
        "session_id": session_id,
        "job_id": job_id,
        "node_key": nk,
        "title": ctx.title,
        "tool": ctx.tool,
        **res,
    }


async def generate_step_guidance_stream(
    *,
    session_id: str,
    node_key: str | None = None,
    refine: str | None = None,
    research: bool | None = None,
    force: bool = False,
    db,
):
    """Streaming sibling of ``generate_step_guidance`` (§17.493).

    Resolves session/node/env (raises ``ValueError`` for a bad session/node so
    the endpoint can map it to HTTP **before** opening the SSE stream), then
    yields the event dicts from ``assist_guide.generate_guidance_stream``.
    """
    from app.config import settings
    from app.modules import assist_guide

    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, metadata, notes
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise ValueError(f"session status {sess['status']!r} cannot generate guidance")
    job_id = str(sess["job_id"])
    # §17.639 — resolve through the anti-echo guard: an explicit node_key is
    # honored, but an auto-resolved pointer never targets a *finished* step (it
    # self-heals forward instead of re-rendering the committed/handed-off one).
    nk = await _resolve_live_node_key(
        session_id=session_id, node_key=node_key,
        current_node_key=sess["current_node_key"], db=db,
    )
    if not nk:
        raise ValueError(
            "no live step to guide; the session's steps are all finished — "
            "run /assist done to see the result, or /assist next to re-check"
        )
    if research is None:
        research = settings.assist_guide_research

    environment = _environment_from_metadata(sess.get("metadata"))
    verbosity = _verbosity_from_metadata(sess.get("metadata"))
    node_row, ctx = await _assemble_ctx_for_node(db=db, job_id=job_id, node_key=nk)
    job_digest = await _job_digest_for(  # §17.650 — whole-project digest
        db=db, job_id=job_id,
        exclude_node_keys={nk, *ctx.upstream_outputs.keys()},
    )
    is_decision = (node_row.get("node_type") or "").lower() == "decision"  # §17.654
    operator_notes = _coerce_notes(sess.get("notes"))

    async for ev in assist_guide.generate_guidance_stream(
        session_id=session_id,
        node_key=nk,
        ctx=ctx,
        node_description=node_row.get("description"),
        research=research,
        refine_hint=refine,
        force=force,
        domain=node_row.get("domain"),
        environment=environment,
        verbosity=verbosity,
        job_digest=job_digest,
        operator_notes=operator_notes,
        is_decision=is_decision,
        db=db,
    ):
        yield ev


async def run_step_research(
    *,
    session_id: str,
    node_key: str | None = None,
    question: str,
    db,
) -> dict:
    """Confirm an operator-supplied question via the research helpers.

    A side query — not persisted to the step's guidance. Resolves the node's
    domain (when a node is in scope) to bias Milvus retrieval, and (§17.650)
    threads the project's own state — brief, environment, and a digest of the
    work already completed on the job — so the answer relays what THIS project
    established rather than a project-blind web lookup.
    """
    from app.modules import assist_guide
    from app.modules.assist_guide import render_environment_block

    if not (question or "").strip():
        raise ValueError("research question is empty")
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, metadata
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    job_id = str(sess["job_id"])
    nk = node_key or sess["current_node_key"]
    domain = None
    if nk:
        drow = (await db.execute(
            text("SELECT domain FROM dag_nodes WHERE job_id = :jid AND node_key = :nk"),
            {"jid": job_id, "nk": nk},
        )).mappings().first()
        domain = (drow or {}).get("domain")

    # §17.650 — assemble project context the ask/research path was blind to.
    job_row = (await db.execute(
        text("SELECT refined_brief FROM jobs WHERE id = :id"),
        {"id": job_id},
    )).mappings().first()
    brief = (job_row or {}).get("refined_brief") or {}
    environment = _environment_from_metadata(sess.get("metadata"))
    digest = await _job_digest_for(db=db, job_id=job_id)
    context_parts: list[str] = []
    goal = (brief or {}).get("description") or (brief or {}).get("title") or ""
    if isinstance(goal, str) and goal.strip():
        context_parts.append(f"## Project goal\n{goal.strip()}")
    env_block = render_environment_block(environment)
    if env_block:
        context_parts.append(env_block)
    if digest:
        context_parts.append(digest)
    job_context = "\n\n".join(context_parts) or None

    res = await assist_guide.research_one(
        question=question, node_key=nk or "?", domain=domain,
        job_context=job_context, context_hint=_kb_hint_from(brief, environment),
    )
    return {"session_id": session_id, "node_key": nk, **res}


async def run_step_fix(
    *,
    session_id: str,
    node_key: str | None = None,
    error: str,
    research: bool | None = None,
    db,
) -> dict:
    """Diagnose an operator-reported error on a step and return corrected steps.

    Resolves the node from ``current_node_key`` when omitted, threads the
    session environment, and auto-records the error to the friction log so
    real blockers are captured for the post-mortem.
    """
    from app.config import settings
    from app.modules import assist_guide

    if not (error or "").strip():
        raise ValueError("error text is empty")
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, metadata
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise ValueError(f"session status {sess['status']!r} cannot run fix")
    job_id = str(sess["job_id"])
    # NB: fix does NOT use the §17.639 anti-echo guard — the operator is
    # diagnosing a problem with the step they just did (which may be terminal),
    # so we must target the referenced/current step, not heal forward to an
    # unrelated future one. The guard is scoped to the guidance path (the thing
    # that re-renders a walkthrough and thus echoes).
    nk = node_key or sess["current_node_key"]
    if not nk:
        raise ValueError(
            "no node_key supplied and session has no current step; "
            "claim one with /assist next first"
        )
    if research is None:
        research = settings.assist_guide_research

    environment = _environment_from_metadata(sess.get("metadata"))
    verbosity = _verbosity_from_metadata(sess.get("metadata"))
    node_row, ctx = await _assemble_ctx_for_node(db=db, job_id=job_id, node_key=nk)
    job_digest = await _job_digest_for(  # §17.653 — troubleshooting is project-aware too
        db=db, job_id=job_id,
        exclude_node_keys={nk, *ctx.upstream_outputs.keys()},
    )

    res = await assist_guide.generate_fix(
        ctx=ctx,
        error_text=error,
        research=research,
        environment=environment,
        node_key=nk,
        domain=node_row.get("domain"),
        verbosity=verbosity,
        job_digest=job_digest,
    )
    # Capture the blocker on the friction trail (best-effort).
    try:
        await record_friction(
            session_id=session_id, node_key=nk,
            note=f"hit error: {error.strip()[:200]}", db=db,
        )
    except Exception as exc:  # never fail the fix on a friction-log hiccup
        logger.warning("assist_fix_friction_record_failed: %s", exc)
    return {"session_id": session_id, "node_key": nk, "title": ctx.title, **res}


async def classify_session_turn(
    *, session_id: str, message: str, node_key: str | None = None, db,
) -> dict:
    """§17.626 — classify an operator's plain-language message against the
    session's current step. Returns ``{intent, evidence, error_text, node_key,
    title}``. Fail-soft: an unresolvable step or classifier error yields
    ``intent='question'`` (the pre-§17.626 guide/refine behavior)."""
    from app.config import settings
    from app.modules import assist_guide

    fallback = {
        "intent": "question", "evidence": "", "error_text": "", "query": "",
        "note_text": "", "note_kind": "note",
        "node_key": node_key, "title": None,
    }
    if not (message or "").strip():
        return fallback
    # §17.626 — master toggle. Off ⇒ every substantive message is a guide/refine
    # turn (pre-§17.626 behavior). The pipeline's deterministic fast-path still
    # handles the obvious verbs (next/skip/pause/finalize) client-side.
    if not settings.assist_nl_turns_enabled:
        return fallback
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess or sess["status"] not in ("active", "paused"):
        return fallback
    nk = node_key or sess["current_node_key"]
    if not nk:
        # No current step to ground on — treat substantive text as a question
        # (the caller will fall through to guidance / usage).
        return fallback
    try:
        node_row, ctx = await _assemble_ctx_for_node(
            db=db, job_id=str(sess["job_id"]), node_key=nk,
        )
    except ValueError:
        return fallback
    res = await assist_guide.classify_turn(
        message=message, title=ctx.title, task_prompt=ctx.base_prompt,
        tool=ctx.tool,
    )
    res["node_key"] = nk
    res["title"] = ctx.title
    return res


# §17.626 — statuses from which a job can be stepped through in Assist Mode.
# Mirrors the guard in assist_agent.start_session / the umbrella handling in
# the router; awaiting_assist is a component parked for hands-on work (§17.624),
# and terminal jobs are re-openable for a hands-on redo (§17.623). §17.682 —
# assisted_paused is included so a PAUSED job stays discoverable by cross-chat
# continuity (start_assist_session resumes it in place); it is non-terminal, so
# it also flows into ASSIST_INPROGRESS_STATUSES below.
ASSIST_ELIGIBLE_STATUSES = (
    "planning", "executing", "running", "blocked", "failed",
    "assisted_executing", "assisted_running", "assisted_paused",
    "awaiting_assist", "completed", "cancelled",
)

# §17.681 — the subset that counts as GENUINELY IN-PROGRESS (not terminal).
# The AUTOMATIC cross-chat continuity surfaces (_reconnect_in_progress,
# _in_progress_banner) must draw from THIS set, not the full eligible list:
# a bare "continue" / topic-matching message must never silently re-open a
# job the user already finished OR that the reaper cancelled after abandonment
# (cleanup turns an abandoned assist session into job='cancelled', which — being
# in ASSIST_ELIGIBLE_STATUSES — otherwise lingered as a reconnect candidate
# forever: the reported "reopens something it never finished" bug). Terminal
# re-open for a deliberate hands-on redo stays available via the EXPLICIT
# `/assist <job_id>` path, which never consults this list.
ASSIST_INPROGRESS_STATUSES = tuple(
    s for s in ASSIST_ELIGIBLE_STATUSES if s not in ("completed", "cancelled")
)


async def list_assist_candidates(
    *, db, limit: int = 25, in_progress: bool = False,
) -> list[dict]:
    """§17.626 — jobs a user could step through in Assist Mode, newest first.

    Returns ``[{job_id, title, status, node_count}]``. Excludes umbrella jobs
    with no DAG of their own (they run autonomously through component children)
    and 0-node jobs (nothing to assist). Used by the natural-language 'start'
    path to map a phrase like 'set up proxmox' to an existing job.

    §17.681 — ``in_progress=True`` restricts to non-terminal jobs
    (``ASSIST_INPROGRESS_STATUSES``). The automatic continuity/banner paths pass
    it so a topic match or "continue" can't resurface a completed/cancelled job;
    the explicit-redo default (``False``) keeps the full re-openable list."""
    statuses = ASSIST_INPROGRESS_STATUSES if in_progress else ASSIST_ELIGIBLE_STATUSES
    rows = (await db.execute(
        text("""
            SELECT j.id, j.title, j.status, j.job_type,
                   (SELECT COUNT(*) FROM dag_nodes n WHERE n.job_id = j.id) AS node_count
              FROM jobs j
             WHERE j.status = ANY(:statuses)
             ORDER BY j.created_at DESC
             LIMIT :lim
        """),
        {"statuses": list(statuses), "lim": limit},
    )).mappings().all()
    out: list[dict] = []
    for r in rows:
        # Umbrella jobs assist via their components, not directly; skip 0-node.
        if (r.get("job_type") or "") == "umbrella":
            continue
        if not r.get("node_count"):
            continue
        out.append({
            "job_id": str(r["id"]),
            "title": r["title"],
            "status": r["status"],
            "node_count": int(r["node_count"]),
        })
    return out


# ── Environment capture (§17.487 — concrete commands, not placeholders) ────


async def _job_digest_for(
    *, db, job_id: str, exclude_node_keys: set[str] | None = None,
) -> str:
    """§17.650 — the project-wide completed-work digest, gated by settings.

    Returns "" when disabled (``assist_job_context_enabled`` /
    ``assist_job_context_max_chars=0``) or when the job has produced nothing
    yet, so callers can unconditionally thread the result. Fail-soft: a digest
    fetch must never break the guidance/research turn.
    """
    from app.config import settings

    if not settings.assist_job_context_enabled or settings.assist_job_context_max_chars <= 0:
        return ""
    try:
        return await assemble_job_digest(
            db=db,
            job_id=job_id,
            exclude_node_keys=exclude_node_keys,
            max_total_chars=settings.assist_job_context_max_chars,
        )
    except Exception as exc:  # noqa: BLE001 — never block the turn on a digest fetch
        logger.warning("assist_job_digest_failed job_id=%s: %s", job_id, exc)
        return ""


def _kb_hint_from(brief: dict, environment: dict) -> str:
    """A short project-entity string to bias local-KB retrieval (§17.650).

    Pulls the brief goal/title and the environment substitution KEYS (not
    values — the keys name the entities, e.g. HOST_A/HOST_B, without leaking
    concrete secrets into the query). Capped so it augments, not dominates,
    the operator's actual question.
    """
    bits: list[str] = []
    goal = (brief or {}).get("description") or (brief or {}).get("title") or ""
    if isinstance(goal, str) and goal.strip():
        bits.append(goal.strip())
    subs = (environment or {}).get("substitutions") or {}
    if isinstance(subs, dict) and subs:
        bits.append(" ".join(str(k) for k in subs.keys()))
    return " ".join(bits)[:300].strip()


def _environment_from_metadata(metadata: Any) -> dict:
    """Pull the `environment` sub-object out of a session's metadata JSONB.

    Tolerates None / str (asyncpg usually hands back a dict for jsonb, but a
    string body is decoded defensively) and always returns a dict with the
    `profile`/`substitutions` shape so callers don't branch.
    """
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    env = (metadata or {}).get("environment") if isinstance(metadata, dict) else None
    if not isinstance(env, dict):
        return {"profile": "", "substitutions": {}}
    return {
        "profile": env.get("profile") or "",
        "substitutions": env.get("substitutions") or {},
    }


_VERBOSITY_LEVELS = ("terse", "normal", "detailed")


def _verbosity_from_metadata(metadata: Any) -> str:
    """§17.499 — the session's walkthrough verbosity (default 'normal')."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    v = (metadata or {}).get("verbosity") if isinstance(metadata, dict) else None
    return v if v in _VERBOSITY_LEVELS else "normal"


async def get_environment(*, session_id: str, db) -> Optional[dict]:
    """Return the session's environment profile + substitutions + verbosity. None if no session."""
    sess = (await db.execute(
        text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        return None
    env = _environment_from_metadata(sess.get("metadata"))
    env["verbosity"] = _verbosity_from_metadata(sess.get("metadata"))
    return env


async def set_environment(
    *,
    session_id: str,
    profile: str | None = None,
    substitutions: dict | None = None,
    verbosity: str | None = None,
    db,
) -> dict:
    """Merge environment facts into `assist_sessions.metadata`.

    `profile` replaces the free-text profile when provided. `substitutions`
    are merged key-by-key (so `/assist env KEY=value` adds one without
    clobbering the rest). `verbosity` (§17.499) sets metadata.verbosity. Read-
    modify-write under the row so we never drop other `metadata` keys.
    """
    if verbosity is not None and verbosity not in _VERBOSITY_LEVELS:
        raise ValueError(f"verbosity must be one of {_VERBOSITY_LEVELS}, got {verbosity!r}")
    sess = (await db.execute(
        text("SELECT metadata FROM assist_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    current = _environment_from_metadata(sess.get("metadata"))
    if profile is not None:
        current["profile"] = profile
    if substitutions:
        merged = dict(current.get("substitutions") or {})
        merged.update(substitutions)
        current["substitutions"] = merged
    # Single jsonb merge patch — environment always, verbosity when given.
    patch: dict[str, Any] = {"environment": current}
    if verbosity is not None:
        patch["verbosity"] = verbosity
    await db.execute(
        text("""
            UPDATE assist_sessions
               SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb),
                   updated_at = NOW()
             WHERE id = :sid
        """),
        {"sid": session_id, "patch": json.dumps(patch)},
    )
    await db.commit()
    current["verbosity"] = verbosity or _verbosity_from_metadata(sess.get("metadata"))
    return current


async def learn_from_submit(
    *, session_id: str, node_key: str, evidence: str, db,
) -> dict:
    """§17.490 — fold concrete values from a submit into the session environment.

    Reads the step's cached walkthrough; if it emitted ``<PLACEHOLDER>`` slots,
    extracts the values the operator actually used from their evidence and
    merges the **new** ones into ``metadata.environment.substitutions`` (never
    overwriting an operator-set or previously-learned value). Returns the dict
    of newly-learned values (for the caller to surface). Best-effort: any
    failure returns ``{}`` and never disturbs the submit.
    """
    from app.modules import assist_guide

    cached = await assist_guide.read_cached_guidance(
        session_id=session_id, node_key=node_key, db=db,
    )
    if not cached or not cached.get("guidance"):
        return {}
    extracted = await assist_guide.extract_substitutions(
        guidance_text=cached["guidance"], evidence=evidence,
    )
    if not extracted:
        return {}
    current = await get_environment(session_id=session_id, db=db) or {}
    existing = current.get("substitutions") or {}
    # Only-add-new: an operator-set or already-learned key wins over a re-read.
    new = {k: v for k, v in extracted.items() if k not in existing}
    if not new:
        return {}
    await set_environment(session_id=session_id, substitutions=new, db=db)
    logger.info(
        "assist_learned_substitutions session_id=%s node_key=%s keys=%s",
        session_id, node_key, ",".join(new.keys()),
    )
    return new


# ── Submit / commit human evidence ───────────────────────────────────────


async def verify_submit_outcome(
    *, session_id: str, node_key: str, evidence: str, db,
) -> Optional[dict]:
    """Judge whether the pasted evidence shows the step succeeded.

    Called by the submit endpoint BEFORE ``submit_step`` so the slow LLM call
    never holds the submit transaction's row lock, and so ``submit_step`` stays
    pure. Reads node + env in a single non-locking query. Returns None when the
    step isn't claimable ('presented') — the endpoint then just calls
    ``submit_step``, which surfaces the real must-claim / no-op path — or a
    verdict dict otherwise. Fail-soft (the underlying verifier never raises).
    """
    row = (await db.execute(
        text("""
            SELECT s.status, ss.metadata,
                   d.title, d.prompt_template, d.tool
              FROM assist_steps s
              JOIN assist_sessions ss ON ss.id = s.session_id
              JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
             WHERE s.session_id = :sid AND s.node_key = :nk
        """),
        {"sid": session_id, "nk": node_key},
    )).mappings().first()
    if not row or row["status"] != "presented":
        return None
    from app.modules import assist_guide
    return await assist_guide.verify_step_success(
        title=row["title"] or node_key,
        task_prompt=row["prompt_template"] or "",
        tool=row["tool"] or "LLM",
        evidence=evidence,
        environment=_environment_from_metadata(row["metadata"]),
    )


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

    Concurrency: requires the step's prior status to be 'presented'.
    Double-submit is a no-op.
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
    if step["status"] not in ("presented",):
        # Idempotent: already-committed submits return current state, not error.
        if step["status"] in ("committed", "skipped"):
            await db.commit()
            return {
                "session_id": session_id,
                "node_key": node_key,
                "status": step["status"],
                "no_op": True,
                # §17.286 — no UPDATE happened on the idempotent path, so
                # divergence can't be detected. Always-False keeps the
                # response shape stable across both branches.
                "mirror_divergence": False,
            }
        if step["status"] == "pending":
            raise ValueError(
                f"must_claim_first: step {node_key} is pending; "
                f"call /assist/{session_id}/next to claim it before submitting"
            )
        raise ValueError(
            f"step {node_key} status {step['status']!r} cannot accept submit"
        )

    job_id = str(step["job_id"])
    if action == "skip":
        step_res = await db.execute(
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
        node_res = await db.execute(
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
        step_res = await db.execute(
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
        node_res = await db.execute(
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

    # Mirror invariant: assist_steps row updated → matching dag_nodes row
    # should also have updated unless it was already terminal. assist_steps
    # is row-locked above so step_res.rowcount is always 1; if dag_nodes
    # rowcount is 0, the corresponding node was already 'done' or 'skipped'
    # from another code path (rare — most likely a stale execute_next_node
    # racing with assist). Log loudly AND surface the divergence in the
    # response dict (§17.286) so the operator can see it without digging
    # through orchestrator logs.
    mirror_divergence = (step_res.rowcount == 1 and node_res.rowcount == 0)
    if mirror_divergence:
        logger.warning(
            "assist_mirror_divergence: session_id=%s node_key=%s "
            "assist_step_status=%s dag_node_already_terminal=true",
            session_id, node_key, committed_status,
        )

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
    else:
        # §17.638 — advance the session pointer off the step we just committed.
        # `current_node_key` was previously moved only by get_next_step (the
        # `/next` claim), so between a commit and the next explicit `/next` it
        # lingered on a *terminal* step. Every conversational turn grounds on
        # `current_node_key` (classify_session_turn / the guide/refine fallback),
        # so the finished step's walkthrough got re-rendered on each turn — the
        # "output is echoing" symptom the operator hit on the homelab job. Point
        # it at the next pending step (computed AFTER _maybe_replan so a
        # selective/full re-plan's resets are reflected) so downstream turns
        # ground on live work. get_next_step re-sets this to the same key when it
        # claims, so the write is idempotent with the claim path.
        await db.execute(
            text(
                "UPDATE assist_sessions SET current_node_key = :nk, "
                "updated_at = NOW() "
                "WHERE id = :sid AND status IN ('active', 'paused')"
            ),
            {"sid": session_id, "nk": next_pending},
        )
        await db.commit()
    return {
        "session_id": session_id,
        "node_key": node_key,
        "status": committed_status,
        "no_op": False,
        "next_node_key": next_pending,
        "replan": replan_result,
        # §17.286 — True when the mirror invariant detected dag_nodes was
        # already terminal (race with execute_next_node). assist_steps was
        # still updated, so the request succeeds; the flag tells operator
        # that the dag_nodes row was NOT touched by this call. Always
        # present (default False) so callers can rely on the key.
        "mirror_divergence": mirror_divergence,
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
               AND status IN ('pending', 'presented')
             ORDER BY node_key LIMIT 1
        """),
        {"sid": session_id},
    )).mappings().first()
    return row["node_key"] if row else None


# §17.639 — a step in one of these statuses is FINISHED; it must never be
# handed back as the session's "current step" to (re)generate a walkthrough for.
# Re-rendering a finished step is the "output is echoing" class (§17.638): the
# pointer lingers on it and every conversational turn replays its walkthrough.
_TERMINAL_STEP_STATUSES = ("committed", "skipped", "handed_off", "escalated")


async def _resolve_live_node_key(
    *, session_id: str, node_key: Optional[str], current_node_key: Optional[str], db,
) -> Optional[str]:
    """Resolve the node a guidance turn should target — never a finished step.

    - An EXPLICIT ``node_key`` is honored verbatim: the operator asked to (re)view
      that specific step, even a completed one (`/assist guide T1`).
    - Otherwise fall back to ``current_node_key`` — but only if it still points at
      LIVE work. If the pointer lingers on a terminal step (handoff marks a node
      ``handed_off`` without advancing the pointer; a commit/skip race; a
      continuity reconnect landing on a finished node), self-heal it forward to
      the next claimable step and persist the corrected pointer, so no path can
      make a walkthrough echo a finished step (§17.638/§17.639). Returns the
      resolved key, or ``None`` when the session has no live step left.

    This is the single choke point every auto-resolved guidance/turn passes
    through, so the anti-echo invariant holds regardless of which upstream path
    left the pointer stale — proactive per-path advances (submit_step) are an
    optimization on top, not the guarantee.
    """
    if node_key:
        return node_key
    nk = current_node_key
    if nk:
        row = (await db.execute(
            text("SELECT status FROM assist_steps "
                 "WHERE session_id = :sid AND node_key = :nk"),
            {"sid": session_id, "nk": nk},
        )).mappings().first()
        if row and row["status"] not in _TERMINAL_STEP_STATUSES:
            return nk  # pointer is live — use it as-is
    # Pointer missing or finished → heal forward to the next claimable step.
    healed = await _next_pending_node_key(session_id=session_id, db=db)
    if healed and healed != nk:
        await db.execute(
            text("UPDATE assist_sessions SET current_node_key = :nk, "
                 "updated_at = NOW() "
                 "WHERE id = :sid AND status IN ('active', 'paused')"),
            {"sid": session_id, "nk": healed},
        )
        await db.commit()
        logger.info(
            "assist_pointer_healed session_id=%s from=%s to=%s",
            session_id, nk, healed,
        )
    return healed


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
    # §17.516 — synthesize a deliverable from the mirrored per-node evidence so
    # the default /results shows a "here's what you built" summary. Before this,
    # the assist path never called _compile_output, leaving compiled_output NULL
    # (the deliverable was only reachable via `/results <id> nodes`).
    # assist_completed=True suppresses the §17.506 PLAN-NOT-EXECUTED banner (the
    # operator DID execute these steps) and prepends a positive assist header.
    # Best-effort: a compile failure must not block session finalization.
    try:
        from app.modules.execution_compile import (
            _compile_output, compute_deliverable_kind,
        )
        compiled, synthesized = await _compile_output(
            str(sess["job_id"]), db, assist_completed=True,
        )
        if compiled:
            kind = await compute_deliverable_kind(  # §17.519 → 'assist_completed'
                str(sess["job_id"]), db, assist_completed=True,
            )
            await db.execute(
                text(
                    "UPDATE jobs SET compiled_output = :co, "
                    "compiled_output_synthesized = :syn, "
                    "deliverable_kind = :dk, updated_at = NOW() "
                    "WHERE id = :jid"
                ),
                {"co": compiled, "syn": synthesized,
                 "dk": kind, "jid": sess["job_id"]},
            )
            # §17.565 — persist the assist deliverable as artifact rows.
            from app.modules.artifacts import persist_job_artifacts
            await persist_job_artifacts(
                str(sess["job_id"]), db, deliverable_kind=kind,
            )
    except Exception as e:  # noqa: BLE001 — finalization must survive compile errors
        logger.warning(
            "assist_compile_failed session_id=%s job_id=%s err=%s",
            session_id, sess["job_id"], e,
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


# ── Notes & additions (§17.654 — capture what the operator raises mid-flow) ─

# Kinds an operator-raised note can be tagged as. Free-form is coerced to
# 'note'; the classifier/pipeline pick a more specific kind when they can.
_NOTE_KINDS = ("note", "addition", "decision", "constraint", "preference")


async def record_note(
    *, session_id: str, text_: str, kind: str = "note",
    node_key: str | None = None, db,
) -> dict | None:
    """§17.654 — append a session-level note/addition. Project-scoped (not tied
    to a step's lifecycle like friction): a new requirement or constraint the
    operator raises should outlive the step it came up on and feed forward into
    every later step's guidance. Appends to ``assist_sessions.notes`` (JSONB
    array). Returns the stored note dict, or None on empty text / missing
    session. Single-statement append; the whole array is re-read cheaply."""
    note_text = (text_ or "").strip()
    if not note_text:
        return None
    k = kind if kind in _NOTE_KINDS else "note"
    # Build the note server-side so the timestamp comes from the DB clock (no
    # datetime import here) and the append stays a single statement.
    res = (await db.execute(
        text("""
            UPDATE assist_sessions
               SET notes = COALESCE(notes, '[]'::jsonb) || jsonb_build_array(
                     jsonb_build_object(
                       'ts', to_char(NOW() AT TIME ZONE 'UTC',
                                     'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                       'kind', CAST(:kind AS text),
                       'node_key', CAST(:nk AS text),
                       'text', CAST(:txt AS text)
                     )),
                   last_activity_at = NOW(),
                   updated_at = NOW()
             WHERE id = :sid
         RETURNING id
        """),
        {"sid": session_id, "kind": k, "nk": node_key, "txt": note_text},
    )).first()
    if res is None:
        await db.rollback()
        return None
    await db.commit()
    return {"kind": k, "node_key": node_key, "text": note_text}


async def assess_note_impact(
    *, session_id: str, note_kind: str, note_text: str, db,
) -> dict | None:
    """§17.677 — after a plan-affecting note is recorded, ask whether it
    invalidates any pending node and, if so, stash a proposal on the session for
    the operator to confirm.

    Gated by ``assist_note_replan_enabled`` and by kind: a generic ``note`` is
    pure feed-forward text (§17.654) and skips analysis. Returns the proposal
    dict (``{note_text, note_kind, proposals, ts}``) when there's something to
    confirm, else ``None``. Fail-soft: any error leaves the note recorded and
    returns ``None`` — the impact pass must never break note-taking.
    """
    from datetime import datetime, timezone

    from app.config import settings
    from app.modules import assist_replan

    if not settings.assist_note_replan_enabled:
        return None
    # A generic 'note' is a reminder, not a plan-shaping requirement.
    if (note_kind or "note") == "note":
        return None
    sess = (await db.execute(
        text("SELECT job_id, status FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess or sess["status"] not in ("active", "paused"):
        return None
    job_id = str(sess["job_id"])
    try:
        impact = await assist_replan.analyze_note_impact(
            db=db, job_id=job_id, note_text=note_text, note_kind=note_kind,
        )
    except Exception as e:  # noqa: BLE001 — never break the note on analysis
        logger.warning("assess_note_impact_failed session_id=%s err=%r", session_id, e)
        return None
    affected = impact.get("affected") or []
    if not affected:
        return None
    proposal = {
        "note_text": note_text,
        "note_kind": note_kind,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proposals": affected,
    }
    # Read-modify-write merge — never clobber other metadata keys (mirrors
    # set_environment). One pending proposal at a time; a fresh note overwrites
    # any unresolved prior one.
    await db.execute(
        text("""
            UPDATE assist_sessions
               SET metadata = COALESCE(metadata, '{}'::jsonb)
                     || CAST(:patch AS jsonb),
                   updated_at = NOW()
             WHERE id = :sid
        """),
        {"sid": session_id, "patch": json.dumps({"pending_replan": proposal})},
    )
    await db.commit()
    logger.info(
        "assist_note_replan_proposed session_id=%s kind=%s affected=%d",
        session_id, note_kind, len(affected),
    )
    return proposal


def _pending_replan_from_metadata(metadata: Any) -> dict | None:
    """§17.677 — pull ``metadata.pending_replan`` out of a session's metadata
    JSONB, tolerating None / a JSON-string body. Returns None when absent."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    pr = (metadata or {}).get("pending_replan") if isinstance(metadata, dict) else None
    return pr if isinstance(pr, dict) else None


async def get_pending_replan(*, session_id: str, db) -> dict | None:
    """§17.677 — the session's un-resolved note-replan proposal, or None."""
    row = (await db.execute(
        text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not row:
        return None
    return _pending_replan_from_metadata(row.get("metadata"))


async def apply_pending_replan(
    *, session_id: str, decision: str, db,
) -> dict:
    """§17.677 — resolve the session's pending note-replan proposal.

    ``decision='apply'`` mutates the pending plan (drop/revise affected nodes)
    then clears the proposal; ``decision='discard'`` clears it and keeps the
    note as-is. Idempotent when nothing is pending. Returns a summary the
    pipeline renders back to the operator.
    """
    from app.modules import assist_replan

    sess = (await db.execute(
        text("SELECT job_id, metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    pending = _pending_replan_from_metadata(sess.get("metadata"))
    if not pending:
        return {"applied": False, "reason": "no_pending"}

    summary: dict
    if decision == "apply":
        result = await assist_replan.apply_note_replan(
            db=db, session_id=session_id, job_id=str(sess["job_id"]),
            proposals=pending.get("proposals") or [],
        )
        summary = {"applied": True, **result}
    else:
        summary = {"applied": False, "discarded": True}

    # Clear the pending proposal regardless of outcome (apply commits inside
    # apply_note_replan; the key-removal is a separate committed statement).
    await db.execute(
        text("""
            UPDATE assist_sessions
               SET metadata = COALESCE(metadata, '{}'::jsonb) - 'pending_replan',
                   updated_at = NOW()
             WHERE id = :sid
        """),
        {"sid": session_id},
    )
    await db.commit()
    return summary


def _coerce_notes(value: Any) -> list[dict]:
    """Normalize an ``assist_sessions.notes`` JSONB value to a list of dicts.
    Tolerates None / a JSON string (defensive decode, mirroring
    _environment_from_metadata) / a already-decoded list."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = []
    return [n for n in (value or []) if isinstance(n, dict)]


async def list_notes(*, session_id: str, db) -> list[dict]:
    """§17.654 — the session's captured notes & additions, oldest first.
    Returns [] for an unknown session or empty list."""
    row = (await db.execute(
        text("SELECT notes FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not row:
        return []
    return _coerce_notes(row.get("notes"))


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
                   AND status IN ('pending', 'presented')
            """),
            {"sid": session_id, "nk": node_key},
        )
    else:
        await db.execute(
            text("""
                UPDATE assist_steps SET status = 'handed_off', updated_at = NOW()
                 WHERE session_id = :sid
                   AND status IN ('pending', 'presented')
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
    from app.modules.execution_agent import execute_all_nodes, execute_next_node

    try:
        if mode == "single":
            # §17.594 — single handoff must run EXACTLY the one node, not the
            # whole remaining DAG. Previously this called the unscoped
            # execute_all_nodes(job_id), which autonomously drained every other
            # 'pending' node — the opposite of "delegate this one step". Claim
            # the target node atomically (pending -> running) and drive it
            # through the per-node autonomous executor via `preclaimed_node`,
            # which skips execute_next_node's own claim. That executor
            # auto-completes the job only if this node was the last remaining
            # one; otherwise the other pending nodes are left untouched and
            # control returns to assist via the restore below. The presented
            # step handed off here is dep-satisfied by assist's DAG walk.
            async with async_session() as dbc:
                claimed = (await dbc.execute(
                    text("""
                        UPDATE dag_nodes
                           SET status = 'running', started_at = NOW()
                         WHERE job_id = :jid AND node_key = :nk
                           AND status = 'pending'
                        RETURNING id, node_key, title, node_type, depends_on,
                                  assigned_model, prompt_template, execution_order,
                                  tool, domain, retry_count, last_verification_reason
                    """),
                    {"jid": job_id, "nk": node_key},
                )).mappings().first()
                await dbc.commit()

            if claimed is None:
                # Node already ran or isn't pending — nothing to hand off.
                yield _sse("assist_handoff_noop", {
                    "session_id": session_id,
                    "node_key": node_key,
                    "reason": "node not pending",
                })
            else:
                yield _sse("node_start", {
                    "node_key": node_key,
                    "title": claimed.get("title"),
                })
                result = await execute_next_node(
                    job_id, preclaimed_node=dict(claimed),
                )
                if result.get("status") in ("done", "skipped"):
                    yield _sse("node_done", {
                        "node_key": node_key,
                        "title": result.get("title"),
                        "verified": result.get("verified", True),
                        "job_complete": result.get("job_complete", False),
                    })
                else:
                    yield _sse("node_failed", {
                        "node_key": node_key,
                        "title": result.get("title"),
                        "error": result.get("error") or result.get("message"),
                        "reason": result.get("reason"),
                    })
        else:
            async for ev in execute_all_nodes(job_id):
                yield ev
    finally:
        # On return, restore assist mode unless all_remaining took over.
        if mode == "single":
            # §17.410 — shield the restore so a client disconnect mid-handoff
            # can't abort it. The bare awaits here used to run unprotected: a
            # CancelledError (SSE disconnect while the handed-off node executes)
            # interrupted the restore, leaving the job stuck in 'executing'
            # instead of 'assisted_executing' until the reaper. Mirrors the
            # cancel-safe finalize in research_state._run_with_session_lifecycle.
            async def _restore_assist_mode() -> None:
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

            try:
                await asyncio.shield(_restore_assist_mode())
            except asyncio.CancelledError:
                # Caller-side cancellation hit while the shielded restore runs;
                # the UPDATE continues on the loop. Re-raise to preserve
                # asyncio cancellation semantics.
                logger.warning(
                    "assist_handoff_restore_cancel_propagated_but_shielded: "
                    "session_id=%s job_id=%s — UPDATE continues on loop",
                    session_id, job_id,
                )
                raise

        # §17.599 — if the handoff drove the job to terminal 'completed',
        # finalize the assist session so /assist/_chatmap stops auto-routing
        # plain chat into a done session and the idle reaper doesn't mislabel
        # it 'abandoned'. Covers both modes: single-mode auto-completes only on
        # the last node (§17.594), all_remaining completes when the DAG
        # finishes. Deliberately does NOT re-compile (the executor already set
        # compiled_output) — this only settles the session row. Shielded so a
        # client disconnect can't strand the session 'active'.
        async def _finalize_session_if_job_done() -> None:
            async with async_session() as db4:
                jstatus = (await db4.execute(
                    text("SELECT status FROM jobs WHERE id = :jid"),
                    {"jid": job_id},
                )).scalar()
                if jstatus == "completed":
                    await db4.execute(
                        text(
                            "UPDATE assist_sessions SET status = 'completed', "
                            "completed_at = NOW(), updated_at = NOW() "
                            "WHERE id = :sid AND status IN ('active', 'paused')"
                        ),
                        {"sid": session_id},
                    )
                    await db4.commit()

        try:
            await asyncio.shield(_finalize_session_if_job_done())
        except asyncio.CancelledError:
            logger.warning(
                "assist_handoff_finalize_cancel_propagated_but_shielded: "
                "session_id=%s job_id=%s", session_id, job_id,
            )
            raise

    yield _sse("assist_handoff_done", {
        "session_id": session_id,
        "node_key": node_key,
        "mode": mode,
    })


# §17.621 (audit #20) — strong refs to fire-and-forget auto-handoff tasks so
# they survive GC (mirrors web.routes / research_agent background-task sets).
_HANDOFF_BACKGROUND_TASKS: set = set()


def spawn_handoff_background(*, session_id: str, node_key: str, mode: str) -> "asyncio.Task":
    """§17.621 (audit #20) — drive ``handoff_step`` to completion on the event
    loop in a background task, consuming (and discarding) its SSE frames.

    This is what makes the ``handoff_policy`` auto values do something: on an
    operator skip with ``auto_on_skip`` / ``auto_all_remaining``, the router
    hands the step to the autonomous executor without blocking the JSON /submit
    response. Uses its OWN short-lived session — ``handoff_step`` only touches
    the passed ``db`` briefly at the start (it commits, releasing the connection)
    and runs the long execution on independent sessions, so nothing is pinned.
    Fire-and-forget + fail-soft: an executor error is logged, never raised.
    """
    async def _run() -> None:
        try:
            async with async_session() as hdb:
                async for _ in handoff_step(
                    session_id=session_id, node_key=node_key, mode=mode, db=hdb,
                ):
                    pass
        except Exception:
            logger.exception(
                "auto_handoff_background_failed: session=%s node=%s mode=%s",
                session_id, node_key, mode,
            )

    task = asyncio.create_task(_run())
    _HANDOFF_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_HANDOFF_BACKGROUND_TASKS.discard)
    return task


def _sse(event_type: str, payload: dict) -> str:
    """SSE wire format. Same shape as research_agent / execution_agent."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
