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
import re
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
                   replan_policy, started_at, last_activity_at, completed_at,
                   notes, metadata
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
    # §17.710d — surface the session memory facts in the roll-up (drop the raw
    # metadata blob from the response; expose only the distilled facts).
    sess_dict = dict(sess)
    env = _environment_from_metadata(sess_dict.pop("metadata", None))
    return {
        **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in sess_dict.items()},
        "step_counts": {r["status"]: r["cnt"] for r in rollup},
        "divergence_count": int(divergence_count),
        "memory_facts": env.get("facts") or [],
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


async def _take_divergence_notice(*, session_id: str, db) -> dict | None:
    """§17.699 — pull a not-yet-surfaced divergence-triggered ``pending_replan``
    and flip it to ``surfaced=True`` so /assist next announces it exactly once.

    Only ``note_kind == 'divergence'`` proposals are auto-surfaced here — the
    note (§17.677) and pivot (§17.693) proposals are surfaced synchronously in
    their own turn, so they're left untouched (returning them here would
    double-announce them). Issues the flip as an un-committed UPDATE; the
    caller's transaction commit persists it, keeping flip-and-show atomic.
    Returns the proposal (pre-flip) for rendering, or None."""
    row = (await db.execute(
        text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not row:
        return None
    pr = _pending_replan_from_metadata(row.get("metadata"))
    if not pr or pr.get("note_kind") != "divergence" or pr.get("surfaced"):
        return None
    flipped = dict(pr)
    flipped["surfaced"] = True
    await db.execute(
        text("""
            UPDATE assist_sessions
               SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb)
             WHERE id = :sid
        """),
        {"sid": session_id, "patch": json.dumps({"pending_replan": flipped})},
    )
    return pr


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

    # §17.699 — one-time proactive surface of a divergence-triggered plan fix.
    # Flips the staged proposal to surfaced (carried by the transaction's
    # commits below) and returns it so we attach it to whatever step we hand
    # back. Attach on EVERY step-returning path so we never flip-without-show
    # (which would silently swallow the notice forever).
    replan_notice = await _take_divergence_notice(session_id=session_id, db=db)

    # §17.645 — one step in flight at a time. Re-present an already-presented,
    # not-yet-submitted step instead of claiming a new (possibly far) node.
    in_flight = await _load_presented_step(session_id=session_id, job_id=job_id, db=db)
    if in_flight is not None:
        await db.commit()
        if replan_notice:
            in_flight["replan_notice"] = replan_notice
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
        fallback = await _load_presented_step(session_id=session_id, job_id=job_id, db=db)
        if fallback is not None and replan_notice:
            fallback["replan_notice"] = replan_notice
        return fallback

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
        # §17.699 — proactive divergence re-plan heads-up (None unless staged).
        "replan_notice": replan_notice,
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
    history: list[dict] | None = None,
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
    conversation = _conversation_block_for(history)  # §17.687

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
        conversation=conversation,
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
    history: list[dict] | None = None,
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
    conversation = _conversation_block_for(history)  # §17.687

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
        conversation=conversation,
        db=db,
    ):
        yield ev


async def run_step_research(
    *,
    session_id: str,
    node_key: str | None = None,
    question: str,
    history: list[dict] | None = None,
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
    conversation = _conversation_block_for(history)  # §17.687
    if conversation:
        context_parts.append(conversation)
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
    history: list[dict] | None = None,
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
        conversation=_conversation_block_for(history),  # §17.687
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
    *, session_id: str, message: str, node_key: str | None = None,
    history: list[dict] | None = None, db,
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
        "node_key": node_key, "title": None, "is_decision": False,
        "is_collect": False,
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
    # §17.689/§17.690 — on a COLLECT step (a decision, or a 'gather' step that
    # asks the operator to provide info), a made/confirmed answer routes to
    # `submit` (→ server-side deliberation). `is_decision` drives the classifier
    # bias; `is_collect` rides back so the pipeline can apply its neutral banner
    # + deterministic confirm backstop to gather steps too.
    kind = _collect_step_kind(node_row.get("node_type"), ctx.base_prompt)
    is_decision = kind == "decision"
    res = await assist_guide.classify_turn(
        message=message, title=ctx.title, task_prompt=ctx.base_prompt,
        tool=ctx.tool, conversation=_conversation_block_for(history),  # §17.687
        is_decision=is_decision,
    )
    res["node_key"] = nk
    res["title"] = ctx.title
    res["is_decision"] = is_decision
    res["is_collect"] = kind is not None
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


def _conversation_block_for(history: list[dict] | None) -> str:
    """§17.687 — render the recent OWUI back-and-forth into a recall block,
    gated by settings. Returns "" when disabled, empty, or on any render error
    so callers can thread the result unconditionally (fail-soft: a history
    render must never break the guidance/fix/research/classify turn)."""
    from app.config import settings
    from app.modules import assist_guide

    if (
        not settings.assist_conversation_context_enabled
        or settings.assist_conversation_context_max_chars <= 0
        or settings.assist_conversation_context_turns <= 0
        or not history
    ):
        return ""
    try:
        turns = settings.assist_conversation_context_turns
        recent = history[-turns:]
        return assist_guide.render_conversation_block(
            recent, max_chars=settings.assist_conversation_context_max_chars,
        )
    except Exception as exc:  # noqa: BLE001 — never block the turn on a render
        logger.warning("assist_conversation_block_failed job_id_unknown: %s", exc)
        return ""


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
        return {"profile": "", "substitutions": {}, "facts": []}
    facts = env.get("facts")
    return {
        "profile": env.get("profile") or "",
        "substitutions": env.get("substitutions") or {},
        # §17.709 — durable observed facts about the operator's system.
        "facts": facts if isinstance(facts, list) else [],
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
    facts: list[str] | None = None,
    db,
) -> dict:
    """Merge environment facts into `assist_sessions.metadata`.

    `profile` replaces the free-text profile when provided. `substitutions`
    are merged key-by-key (so `/assist env KEY=value` adds one without
    clobbering the rest). `verbosity` (§17.499) sets metadata.verbosity.
    `facts` (§17.709) are APPENDED to the durable facts ledger, de-duplicated
    (case-insensitive) against what's there, oldest-dropped-first to the
    `assist_facts_max` cap. Read-modify-write under the row so we never drop
    other `metadata` keys.
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
    if facts:
        from app.config import settings as _s
        existing = list(current.get("facts") or [])
        seen = {str(f).strip().lower() for f in existing}
        for f in facts:
            t = str(f).strip()
            if t and t.lower() not in seen:
                existing.append(t)
                seen.add(t.lower())
        # Cap: keep the most recent (oldest drop first).
        current["facts"] = existing[-int(_s.assist_facts_max):]
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


# §17.701 — a pasted interactive-shell prompt (e.g. `root@pve:~#`) reveals the
# operator's ACTUAL execution context: one interactive shell on a named host
# (typically the Proxmox web console). Anchored on a leading prompt line so it
# doesn't fire on prose that merely mentions an email-like `user@host`.
_SHELL_PROMPT_RE = re.compile(r"(?m)^\s*([A-Za-z_][\w.-]*)@([\w.-]+):[^\n#$]*[#$]")

# §17.703 — sentinel prefix marking a profile string that WE auto-captured (vs.
# one the operator set explicitly via `/assist env`). Change-detection only ever
# replaces an auto-captured profile; an operator's explicit profile is sacred.
_EXEC_CTX_SENTINEL = "Operator runs commands as "


def _detect_shell_context(evidence: str) -> Optional[tuple[str, str]]:
    """§17.701 — infer the operator's execution context from a pasted shell
    prompt. Returns ``(user, host)`` when a prompt line is present, else None.
    Anchored on a leading prompt line so it doesn't fire on prose that merely
    mentions an email-like ``user@host``."""
    m = _SHELL_PROMPT_RE.search(evidence or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def _exec_context_profile(user: str, host: str) -> str:
    """Build the single-interactive-shell profile string for ``user@host``.

    Recording it makes later guidance single-shell-safe with the REAL host/user
    (reinforcing §17.700's runbook rule at the per-session level): the model is
    told the operator pastes a block into ONE shell on that host, not a
    multi-terminal SSH setup."""
    return (
        f"{_EXEC_CTX_SENTINEL}{user}@{host} in ONE interactive shell "
        f"(the host's console / a single SSH session), pasting a command block "
        f"and pasting the output back — NOT a multi-terminal setup. Keep every "
        f"step runnable in that single shell."
    )


async def capture_execution_context(
    *, session_id: str, evidence: str, db,
) -> Optional[dict]:
    """§17.703 — the deterministic execution-environment monitor.

    Detects the operator's execution context (``user@host`` in ONE interactive
    shell) from a pasted prompt in their evidence and keeps
    ``metadata.environment.profile`` in sync with it. Runs on EVERY submit —
    decoupled from the substitution-learning valve and from the success verdict
    — so it captures on a failed/error paste too (that's still the operator's
    real shell). Returns ``{user, host, changed}`` when it wrote a profile, else
    None. Fail-soft: any error returns None and never disturbs the submit.

    Retention semantics (why this is a *monitor*, not a one-shot capture):
      • profile empty            → capture it.
      • profile is a prior auto-capture (``_EXEC_CTX_SENTINEL``) that names a
        DIFFERENT host/user → the operator moved (e.g. ``root@pve`` →
        ``root@ct100``); replace it.
      • profile already names this exact ``user@host`` → no-op.
      • profile was set explicitly by the operator (``/assist env``, no
        sentinel) → leave it; an explicit profile outranks an inferred one
        (mirrors the only-add-new rule in :func:`learn_from_submit`).
    """
    try:
        detected = _detect_shell_context(evidence)
        if not detected:
            return None
        user, host = detected
        env = await get_environment(session_id=session_id, db=db) or {}
        current = (env.get("profile") or "").strip()
        marker = f"{_EXEC_CTX_SENTINEL}{user}@{host} "
        if marker in current:
            return None  # already recorded this exact context
        # Respect an operator-set (non-sentinel, non-empty) profile.
        if current and not current.startswith(_EXEC_CTX_SENTINEL):
            return None
        changed = bool(current)  # a prior auto-capture named a different host
        await set_environment(
            session_id=session_id, profile=_exec_context_profile(user, host), db=db,
        )
        logger.info(
            "assist_%s_shell_context session_id=%s ctx=%s@%s",
            "switched" if changed else "captured", session_id, user, host,
        )
        return {"user": user, "host": host, "changed": changed}
    except Exception as e:  # noqa: BLE001 — context capture must never break submit
        logger.debug(
            "shell_context_capture_failed session_id=%s err=%r", session_id, e,
        )
        return None


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

    §17.701 — also captures the operator's execution context (single interactive
    shell) from a pasted prompt, once, when unset — independent of placeholder
    substitutions.
    """
    from app.modules import assist_guide

    # §17.701/703 — keep the operator's execution context (single interactive
    # shell, `user@host`) in sync. Delegated to the standalone monitor, which is
    # idempotent and fail-soft. The router ALSO calls it unconditionally on every
    # submit (§17.703); this call keeps CLI/other callers of learn_from_submit
    # covered without a second code path.
    await capture_execution_context(
        session_id=session_id, evidence=evidence, db=db,
    )

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


async def capture_session_facts(
    *, session_id: str, node_key: str, evidence: str, db,
) -> list[str]:
    """§17.709 — distill durable facts about the operator's ACTUAL system from a
    submit's evidence and append them to the session facts ledger
    (``metadata.environment.facts``), which renders into EVERY later step's
    guidance + decision-deliberation context.

    This is the retention layer substitution-learning misses: an audit /
    inventory / gather step carries real system state but has no
    ``<PLACEHOLDER>`` tokens, so ``learn_from_submit`` retained nothing and later
    decisions fabricated assumptions ("Assumption: Fresh Proxmox VE server"). Now
    the facts survive independently of placeholders and of digest truncation.
    Best-effort: any failure returns ``[]`` and never disturbs the submit.
    """
    from app.config import settings
    from app.modules import assist_guide

    if not settings.assist_capture_facts_enabled or not (evidence or "").strip():
        return []
    try:
        row = (await db.execute(
            text("""
                SELECT d.title, d.prompt_template
                  FROM assist_steps s
                  JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
                 WHERE s.session_id = :sid AND s.node_key = :nk
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().first()
        facts = await assist_guide.distill_facts(
            evidence=evidence,
            title=(row or {}).get("title") or "",
            task_prompt=(row or {}).get("prompt_template") or "",
        )
        if not facts:
            return []
        await set_environment(session_id=session_id, facts=facts, db=db)
        logger.info(
            "assist_captured_facts session_id=%s node_key=%s n=%d",
            session_id, node_key, len(facts),
        )
        return facts
    except Exception as e:  # noqa: BLE001 — fact capture must never break submit
        logger.debug(
            "capture_session_facts_failed session_id=%s err=%r", session_id, e,
        )
        return []


async def check_submit_grounding(
    *, session_id: str, node_key: str, evidence: str, db,
) -> Optional[dict]:
    """§17.710c — warn-only grounding gate. Does this submit's result contradict
    what we already know about the operator's system? Reads the session
    environment (facts/provided/profile) + notes as memory, asks the grounding
    checker, and returns ``{reason}`` ONLY on a contradiction (else None) so the
    caller can surface a non-blocking warning. Gated on the master + grounding
    valves; fail-soft. Run BEFORE this submit's own facts are folded in, so the
    result can't be judged consistent with its own claims."""
    from app.config import settings
    from app.modules import assist_guide

    if not (settings.assist_unified_memory_enabled and settings.assist_umem_grounding):
        return None
    if not (evidence or "").strip():
        return None
    try:
        env = await get_environment(session_id=session_id, db=db) or {}
        notes = None
        sess = (await db.execute(
            text("SELECT notes FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if sess:
            notes = _coerce_notes(sess.get("notes"))
        verdict = await assist_guide.check_grounding(
            evidence=evidence, environment=env, operator_notes=notes,
        )
        if verdict.get("contradicts"):
            logger.info(
                "assist_grounding_contradiction session_id=%s node_key=%s reason=%r",
                session_id, node_key, verdict.get("reason"),
            )
            return {"reason": verdict.get("reason") or ""}
        return None
    except Exception as e:  # noqa: BLE001 — never block a submit on the gate
        logger.debug(
            "check_submit_grounding_failed session_id=%s err=%r", session_id, e,
        )
        return None


# ── Unconditional per-turn derive (§17.715 — review + log EVERY message) ────

# Strong refs so fire-and-forget derive tasks aren't GC'd mid-flight; tests
# await them via drain_derive_tasks(). Same pattern as assist_replan's
# _BACKGROUND_TASKS.
_DERIVE_TASKS: set = set()

# Pure control tokens carry no durable info — skip the extraction call for a
# bare "yes"/"next"/"ok" (a real plan change is always ≥2 words: "drop it",
# "use wireguard"). Cheap pre-filter only; anything with 2+ words goes through.
_TRIVIAL_TURN = {
    "yes", "no", "y", "n", "ok", "okay", "next", "skip", "done", "pause",
    "resume", "continue", "stop", "go", "sure", "yep", "yeah", "nope",
    "thanks", "thx", "ty", "cool", "great", "perfect", "confirm", "confirmed",
}


def _norm_note(text_: str) -> str:
    return " ".join((text_ or "").lower().split())


async def derive_turn_memory(
    *, session_id: str, node_key: str | None, message: str, db,
) -> dict:
    """§17.715 — the unconditional review the trigger-gated paths miss: extract
    any durable, plan-relevant memory from ONE operator message and LOG it into
    the notes/facts guidance injects. Dedup-safe (won't restate standing memory).
    Silent — does NOT surface an interactive re-plan (that stays on the §17.693
    pivot path). Gated on the master + derive valves; fail-soft (returns a
    summary dict, never raises).

    This closes the gap §17.710a left: capture was made unconditional, but the
    derive/review step was still gated on intent (skip/question≥6w pivots,
    explicit notes, submit-facts). A plan change stated in a message routed to
    ask/fix/etc. was captured raw yet never became memory that shapes later
    steps. Now every message is reviewed."""
    from app.config import settings
    from app.modules import assist_guide

    result = {"notes_added": 0, "facts_added": 0}
    if not (settings.assist_unified_memory_enabled and settings.assist_umem_derive):
        return result
    msg = (message or "").strip()
    if len(msg.split()) < 2 or msg.lower() in _TRIVIAL_TURN:
        return result
    try:
        sess = (await db.execute(
            text("SELECT status, notes, metadata FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess or sess["status"] not in ("active", "paused"):
            return result
        existing_notes = _coerce_notes(sess.get("notes"))
        env = _environment_from_metadata(sess.get("metadata"))
        known_note_texts = [n.get("text", "") for n in existing_notes if n.get("text")]
        known_facts = [str(f) for f in (env.get("facts") or [])]
        derived = await assist_guide.distill_turn_memory(
            message=msg, known_notes=known_note_texts, known_facts=known_facts,
        )
        # Dedup notes against what's already recorded (exact/substring, both
        # ways) so a restated standing decision doesn't pile up on every turn.
        seen = {_norm_note(t) for t in known_note_texts}
        for n in derived.get("notes") or []:
            cand = _norm_note(n["text"])
            if not cand or cand in seen:
                continue
            if any(cand in s or s in cand for s in seen):
                continue
            stored = await record_note(
                session_id=session_id, text_=n["text"], kind=n["kind"],
                node_key=node_key, db=db,
            )
            if stored:
                seen.add(cand)
                result["notes_added"] += 1
        # Facts: set_environment already dedups case-insensitively + caps.
        new_facts = [f for f in (derived.get("facts") or [])]
        if new_facts:
            await set_environment(session_id=session_id, facts=new_facts, db=db)
            result["facts_added"] = len(new_facts)
        if result["notes_added"] or result["facts_added"]:
            logger.info(
                "assist_derived_turn_memory session_id=%s notes=+%d facts=+%d",
                session_id, result["notes_added"], result["facts_added"],
            )
    except Exception as e:  # noqa: BLE001 — the scribe must never break the turn
        logger.debug("derive_turn_memory_failed session_id=%s err=%r", session_id, e)
    return result


async def _derive_turn_memory_bg(
    *, session_id: str, node_key: str | None, message: str,
) -> None:
    """Background worker: open a fresh session (the request session is gone by
    the time this runs) and derive. Swallows every exception — a scribe hiccup
    surfaces only in logs, never as an unhandled-task warning."""
    try:
        async with async_session() as bg_db:
            await derive_turn_memory(
                session_id=session_id, node_key=node_key, message=message, db=bg_db,
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("derive_turn_memory_bg_failed session_id=%s err=%r", session_id, e)


def schedule_derive_turn_memory(
    *, session_id: str, node_key: str | None, message: str,
) -> None:
    """§17.715 — fire-and-forget the per-turn derive off the request path so the
    conversation never waits on it (same posture as context_only divergence).
    No-op unless the derive valve is on. Strong ref via ``_DERIVE_TASKS`` so the
    task isn't GC'd before it finishes."""
    from app.config import settings
    if not (settings.assist_unified_memory_enabled and settings.assist_umem_derive):
        return
    task = asyncio.create_task(
        _derive_turn_memory_bg(
            session_id=session_id, node_key=node_key, message=message,
        )
    )
    _DERIVE_TASKS.add(task)
    task.add_done_callback(_DERIVE_TASKS.discard)


async def drain_derive_tasks() -> None:
    """Await all in-flight derive tasks. Tests call this between a /turn and any
    assertion on the derived notes/facts; production never waits."""
    if not _DERIVE_TASKS:
        return
    await asyncio.gather(*list(_DERIVE_TASKS), return_exceptions=True)


# ── Unified session memory (§17.710a — lossless raw capture) ──────────────


async def ingest_turn(
    *, session_id: str, role: str, kind: str, content: str,
    node_key: str | None = None, evidence_kind: str | None = None, db,
) -> bool:
    """§17.710a — append a raw turn to the append-only ``assist_turns``
    transcript, UNCONDITIONALLY and BEFORE any intent classification.

    This is the lossless capture layer the narrow retention channels missed:
    whatever didn't match a channel's trigger (an audit paste with no
    placeholders, a message the classifier mislabeled) still lands here, so
    Stage B can derive ``session_memory`` from the transcript. Gated on the
    master + capture valves; commits its own insert so a later rollback in the
    caller can't lose the turn; fail-soft (never disturbs the caller). Returns
    True iff a row was written."""
    from app.config import settings

    if not (settings.assist_unified_memory_enabled and settings.assist_umem_capture):
        return False
    # A skip carries no content but is still a real turn worth recording.
    if not (content or "").strip() and kind != "skip":
        return False
    try:
        # INSERT…SELECT pulls the session's job_id and no-ops if the session is
        # unknown (no matching row → nothing inserted).
        res = await db.execute(
            text("""
                INSERT INTO assist_turns
                    (session_id, job_id, node_key, role, kind, content, evidence_kind)
                SELECT :sid, s.job_id, :nk, :role, :kind, :content, :ek
                  FROM assist_sessions s WHERE s.id = :sid
            """),
            {"sid": session_id, "nk": node_key, "role": role, "kind": kind,
             "content": content or "", "ek": evidence_kind},
        )
        await db.commit()
        return bool(getattr(res, "rowcount", 0))
    except Exception as e:  # noqa: BLE001 — capture must never break the turn
        logger.debug("ingest_turn_failed session_id=%s err=%r", session_id, e)
        return False


async def list_turns(*, session_id: str, limit: int = 200, db) -> list[dict]:
    """§17.710a — the session's raw transcript, oldest-first. Backs GET
    /assist/{sid}/turns and (Stage B) session_memory consolidation."""
    rows = (await db.execute(
        text("""
            SELECT id, node_key, role, kind, content, evidence_kind, created_at
              FROM assist_turns WHERE session_id = :sid
             ORDER BY created_at, id LIMIT :lim
        """),
        {"sid": session_id, "lim": int(limit)},
    )).mappings().all()
    return [dict(r) for r in rows]


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
                   d.title, d.prompt_template, d.tool, d.node_type
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
        # §17.688 — a decision node is judged on the CHOICE, not the downstream
        # concrete artifact (its task text names a table/config later steps apply).
        is_decision=(row.get("node_type") or "").lower() == "decision",
    )


# §17.690 — a GATHER step's task asks the operator to PROVIDE several specific
# pieces of information ("Operator provides: exact model, disk inventory,
# GPU(s), NIC models"). The planner phrases these as "Operator provides /
# supplies / must provide …". Detection is intentionally a touch loose: a false
# match just resolves in one turn like a normal submit (no harm), whereas a miss
# re-opens the reported bug (a partial answer commits with the rest missing).
_GATHER_TASK_RE = re.compile(
    r"operator\s+(?:provides?|supplies|must\s+provide|will\s+provide|"
    r"needs?\s+to\s+provide|to\s+provide)",
    re.I,
)


def _collect_step_kind(node_type: str | None, task_prompt: str) -> Optional[str]:
    """§17.689/§17.690 — classify a step as a COLLECT step whose deliverable the
    operator supplies across one or more turns. Returns ``'decision'`` (a choice
    / concrete artifact), ``'gather'`` (specific requested information), or
    ``None`` (not a collect step — commit normally)."""
    if (node_type or "").lower() == "decision":
        return "decision"
    if _GATHER_TASK_RE.search(task_prompt or ""):
        return "gather"
    return None


async def build_inputs_checklist(*, session_id: str, db) -> dict:
    """§17.707 — the operator-input checklist for the session's plan.

    A read-only, LIVE view of what the engine needs FROM the operator across the
    whole component: the decisions they must make (``decision`` nodes) and the
    information they must supply (``gather`` steps, per ``_collect_step_kind``),
    each marked done/open from the node's live status, plus the concrete values
    learned so far (``environment.substitutions``). Because it reflects live
    state, it "fills in" as the operator works — no separate accumulator to drift
    out of sync. Returns ``{session_id, items[], provided{}, open_count, total}``.
    """
    sess = (await db.execute(
        text("SELECT job_id, metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    rows = (await db.execute(
        text("""
            SELECT node_key, node_type, title, prompt_template, status
              FROM dag_nodes WHERE job_id = :jid ORDER BY node_key
        """),
        {"jid": str(sess["job_id"])},
    )).mappings().all()
    items: list[dict] = []
    for r in rows:
        kind = _collect_step_kind(r["node_type"], r["prompt_template"] or "")
        if not kind:
            continue
        items.append({
            "node_key": r["node_key"],
            "kind": kind,  # 'decision' | 'gather'
            "title": r["title"] or r["node_key"],
            "done": (r["status"] or "").lower() == "done",
        })
    env = _environment_from_metadata(sess.get("metadata"))
    return {
        "session_id": session_id,
        "items": items,
        "provided": env.get("substitutions") or {},
        # §17.709 — durable facts learned about the operator's system so far.
        "facts": env.get("facts") or [],
        "open_count": sum(1 for i in items if not i["done"]),
        "total": len(items),
    }


async def run_step_decision(
    *, session_id: str, node_key: str, message: str,
    history: list[dict] | None = None, db,
) -> Optional[dict]:
    """§17.689/§17.690 — one turn of a COLLECT step's deliberation (a decision
    node, or a 'gather' step that asks the operator to provide specific info).

    Called by the submit endpoint BEFORE committing. Returns:
      - ``None`` when deliberation does not apply — the step isn't claimable
        ('presented'), the node isn't a ``decision``, or the deliberation model
        failed/gave an unusable answer — so the caller falls back to the plain
        single-turn commit (a decision is still judged correctly by §17.688).
      - ``{status: 'needs_input', message}`` — do NOT commit; show the message
        and keep the step open for the operator's next turn.
      - ``{status: 'resolved', decision_record, message}`` — commit the
        ``decision_record`` (the complete concrete artifact) as the step output.

    Fail-soft throughout: a deliberation hiccup must never trap the operator.
    """
    from app.config import settings
    if not settings.assist_decision_deliberation_enabled:
        return None
    row = (await db.execute(
        text("""
            SELECT s.status, s.job_id, ss.metadata, ss.notes,
                   d.title, d.prompt_template, d.node_type
              FROM assist_steps s
              JOIN assist_sessions ss ON ss.id = s.session_id
              JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
             WHERE s.session_id = :sid AND s.node_key = :nk
        """),
        {"sid": session_id, "nk": node_key},
    )).mappings().first()
    if not row or row.get("status") != "presented":
        return None
    task_prompt = row.get("prompt_template") or ""
    kind = _collect_step_kind(row.get("node_type"), task_prompt)
    if kind is None:
        return None
    job_id = str(row["job_id"])
    environment = _environment_from_metadata(row["metadata"])
    operator_notes = _coerce_notes(row["notes"])
    job_digest = await _job_digest_for(
        db=db, job_id=job_id, exclude_node_keys={node_key},
    )
    conversation = _conversation_block_for(history)
    from app.modules import assist_guide
    res = await assist_guide.deliberate_decision(
        title=row["title"] or node_key,
        task_prompt=task_prompt,
        environment=environment,
        job_digest=job_digest,
        operator_notes=operator_notes,
        conversation=conversation,
        latest_message=message,
        kind=kind,
    )
    status = res.get("status")
    if status == "needs_input" and (res.get("message") or "").strip():
        return {"status": "needs_input", "message": res["message"].strip(),
                "collect_kind": kind}
    if status == "resolved" and (res.get("decision_record") or "").strip():
        return {
            "status": "resolved",
            "decision_record": res["decision_record"].strip(),
            "message": (res.get("message") or "").strip(),
            "collect_kind": kind,
        }
    # error / unusable (e.g. resolved with no record) → plain single-turn commit.
    return None


async def submit_step(
    *,
    session_id: str,
    node_key: str,
    evidence: str,
    evidence_kind: str = "text",
    evidence_meta: dict | None = None,
    action: str = "submit",
    friction_note: str | None = None,
    verdict_failed: bool = False,
    db,
) -> dict:
    """Record human evidence for one step. Mirrors to `dag_nodes.output_text`.

    `action` is "submit" (mark dag_node done) or "skip" (mark skipped).

    `verdict_failed` (§17.708) — the §17.487 success verifier judged this
    evidence a FAILURE (a command errored / non-zero exit / "Connection
    refused"). When True, divergence detection is skipped: a failed step is a
    RECOVER situation (fix + retry), not a semantic divergence that should
    re-plan downstream steps. Passing it prevents the confusing double-signal
    ("⚠️ this may have failed" + "⚠️ your result diverges, re-plan…").

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
    # §17.708 — and NOT when the step's evidence was judged a failure: a command
    # that errored (e.g. Proxmox `ipcc_send_rec: Connection refused` = pve-cluster
    # down) is a recover-and-retry situation, not a semantic divergence that
    # should re-plan the downstream steps. Detecting "the failed output doesn't
    # meet the task's intent" and proposing to drop later steps is exactly the
    # confusing behavior the operator kept hitting.
    replan_result = None
    if action == "submit" and not verdict_failed:
        replan_result = await _maybe_replan(
            session_id=session_id, job_id=job_id,
            node_key=node_key, evidence=evidence, db=db,
        )
    elif action == "submit" and verdict_failed:
        logger.info(
            "assist_replan_skipped_failed_verdict session_id=%s node_key=%s",
            session_id, node_key,
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
            SELECT title, prompt_template, node_type
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
        # §17.688 — a decision node's concise choice is not divergence from its
        # concrete-artifact task text (that artifact is applied by later steps).
        is_decision=(node["node_type"] or "").lower() == "decision",
    )


async def _next_pending_node_key(*, session_id: str, db) -> Optional[str]:
    # §17.693 — order by the DAG's execution_order, NOT the raw node_key. A plain
    # `ORDER BY node_key` sorts lexically, so after T1 the "next" pending step was
    # reported as `T10` ("T1" < "T10" < "T2") — the wrong step, shown in every
    # "Moving on to X" line AND stashed as the remembered node_key. Mirror the
    # ordering get_next_step / _load_presented_step use so the reported next step
    # matches the one actually presented.
    row = (await db.execute(
        text("""
            SELECT s.node_key
              FROM assist_steps s
              JOIN dag_nodes d ON d.job_id = s.job_id AND d.node_key = s.node_key
             WHERE s.session_id = :sid
               AND s.status IN ('pending', 'presented')
             ORDER BY d.execution_order NULLS LAST, s.node_key
             LIMIT 1
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


# §17.702 — assist work is operator-executed on real systems, so it clears the
# exemplar grounding floor by construction (unlike unverified autonomous LLM
# output, which the 0.85 floor exists to filter). High but not 1.0 so a stricter
# floor can still be set to opt assist work out.
_ASSIST_EXEMPLAR_GROUNDING = 0.9


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
            # §17.702 — feed the learning flywheel from ASSIST too. A component
            # finished in assist mode is OPERATOR-EXECUTED on real systems — the
            # strongest kind of "proven solution" the exemplar corpus wants — but
            # the flywheel hook lived ONLY on the autonomous execution path
            # (execution_agent), so the operator's whole workflow never became a
            # reusable exemplar even with the valve on. Operator-grounded ⇒ pass a
            # high grounding score; maybe_ingest_exemplar still gates on the opt-in
            # valve + plan_only/empty-output. Gate on ≥1 committed step so a
            # walk that was entirely skipped (no real executed work) isn't learned.
            try:
                committed = (await db.execute(
                    text("SELECT count(*) FROM assist_steps "
                         "WHERE session_id = :sid AND status = 'committed'"),
                    {"sid": session_id},
                )).scalar() or 0
                if committed:
                    _dom = (await db.execute(
                        text("SELECT refined_brief->>'domain' FROM jobs WHERE id = :jid"),
                        {"jid": sess["job_id"]},
                    )).scalar() or "eng"
                    from app.modules.flywheel import maybe_ingest_exemplar
                    await maybe_ingest_exemplar(
                        job_id=str(sess["job_id"]), compiled_output=compiled,
                        deliverable_kind=kind,
                        grounding_score=_ASSIST_EXEMPLAR_GROUNDING, domain=_dom,
                    )
            except Exception as e:  # noqa: BLE001 — ingest is best-effort
                logger.warning(
                    "assist_exemplar_ingest_failed job_id=%s err=%s",
                    sess["job_id"], e,
                )
    except Exception as e:  # noqa: BLE001 — finalization must survive compile errors
        logger.warning(
            "assist_compile_failed session_id=%s job_id=%s err=%s",
            session_id, sess["job_id"], e,
        )
    # §17.701 — a component finished via assist must roll its umbrella up. The
    # umbrella parked at 'awaiting_assist' when its children reached the hands-on
    # gate; without this it stays there forever once every component is done via
    # assist (the cleanup safety net only re-finalizes 'aggregating' umbrellas),
    # so the unified whole-project deliverable would never assemble. Best-effort:
    # a rollup failure must not block the operator's session from finalizing.
    parent_id = (await db.execute(
        text("SELECT parent_job_id FROM jobs WHERE id = :jid"),
        {"jid": sess["job_id"]},
    )).scalar()
    if parent_id:
        try:
            from app.modules.decomposition import _rollup_umbrella
            await _rollup_umbrella(db, str(parent_id))
        except Exception as e:  # noqa: BLE001 — never block finalize on rollup
            logger.warning(
                "assist_umbrella_rollup_failed job_id=%s parent=%s err=%s",
                sess["job_id"], parent_id, e,
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
    return await _stage_replan_proposal(
        session_id=session_id, note_text=note_text, note_kind=note_kind,
        affected=affected, db=db,
    )


async def _stage_replan_proposal(
    *, session_id: str, note_text: str, note_kind: str,
    affected: list, db,
) -> dict:
    """§17.677/§17.693 — stash a pending_replan proposal on the session for the
    operator to confirm. Read-modify-write merge so other metadata keys survive;
    one pending proposal at a time (a fresh one overwrites any unresolved prior).
    """
    from datetime import datetime, timezone
    proposal = {
        "note_text": note_text,
        "note_kind": note_kind,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proposals": affected,
    }
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
        "assist_replan_proposed session_id=%s kind=%s affected=%d",
        session_id, note_kind, len(affected),
    )
    return proposal


async def detect_reroute(
    *, session_id: str, message: str, db,
) -> dict | None:
    """§17.693 — semantic pivot detection for a substantive turn the classifier
    read as ``skip`` / ``question``.

    Deterministic pivot patterns miss references to the operator's ACTUAL
    situation ("I already have Proxmox installed, we only need to remove the old
    containers") — lexically unremarkable, but they invalidate whole branches of
    the plan. This runs the §17.677 impact analyzer (the reliable semantic
    component) over the PENDING nodes; when ≥1 is affected, it records the
    message as a plan note (feed-forward) AND stages a pending_replan, returning
    the proposal for the pipeline to surface. Returns None when nothing is
    affected — a pure dry run with NO side effects, so the caller proceeds with
    the original intent. Fail-soft: any error → None (never trap the turn).
    """
    from app.config import settings
    from app.modules import assist_replan

    if (not settings.assist_pivot_detect_enabled
            or not settings.assist_note_replan_enabled
            or not (message or "").strip()):
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
            db=db, job_id=job_id, note_text=message, note_kind="decision",
        )
    except Exception as e:  # noqa: BLE001 — never trap the turn on analysis
        logger.warning("detect_reroute_failed session_id=%s err=%r", session_id, e)
        return None
    affected = impact.get("affected") or []
    if not affected:
        return None  # not a pivot — caller proceeds with skip/question
    # It reshapes the plan. Record the operator's message as a decision note so
    # it feeds forward, then stage the proposal for surface-and-ask.
    try:
        await record_note(
            session_id=session_id, text_=message, kind="decision",
            node_key=None, db=db,
        )
    except Exception as e:  # noqa: BLE001 — the replan is what matters
        logger.warning("detect_reroute_note_failed session_id=%s err=%r", session_id, e)
    return await _stage_replan_proposal(
        session_id=session_id, note_text=message, note_kind="decision",
        affected=affected, db=db,
    )


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
