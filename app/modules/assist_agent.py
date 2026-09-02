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
import time
import uuid as _uuid
from dataclasses import asdict, dataclass
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
            RETURNING id, job_id, status, handoff_policy, replan_policy,
                      (xmax = 0) AS inserted
        """),
        {"jid": job_id, "hp": handoff_policy, "rp": replan_policy},
    )).mappings().first()
    session_id = str(sess_row["id"])

    # §17.723 — seed a NEW component session's environment from its most
    # recently active sibling under the same umbrella. The facts ledger /
    # substitutions / execution profile live per-SESSION, but the components of
    # one umbrella run against the SAME physical system — pre-§17.723 every
    # component started blind and the operator had to re-teach known state
    # ("we are using a zfspool, it should be in your notes"). Only fires on a
    # brand-new session (never overwrites state a session already gathered).
    if sess_row.get("inserted"):
        sibling = (await db.execute(
            text("""
                SELECT s.metadata->'environment' AS env
                  FROM assist_sessions s
                  JOIN jobs sj ON sj.id = s.job_id
                 WHERE sj.parent_job_id = (
                           SELECT parent_job_id FROM jobs WHERE id = :jid
                       )
                   AND sj.parent_job_id IS NOT NULL
                   AND s.job_id <> :jid
                   AND s.metadata ? 'environment'
                 ORDER BY s.last_activity_at DESC
                 LIMIT 1
            """),
            {"jid": job_id},
        )).mappings().first()
        if sibling and sibling["env"]:
            env = sibling["env"]
            if isinstance(env, str):
                env = json.loads(env)
            if isinstance(env, dict) and any(
                env.get(k) for k in ("profile", "facts", "substitutions")
            ):
                await db.execute(
                    text("""
                        UPDATE assist_sessions
                           SET metadata = COALESCE(metadata, '{}'::jsonb)
                                          || CAST(:patch AS jsonb)
                         WHERE id = :sid
                    """),
                    {"sid": session_id, "patch": json.dumps({"environment": env})},
                )
                logger.info(
                    "assist_env_seeded_from_sibling session_id=%s job_id=%s "
                    "facts=%d subs=%d",
                    session_id, job_id, len(env.get("facts") or []),
                    len(env.get("substitutions") or {}),
                )

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


#  §17.811 — terminal assist step statuses (a step that has moved on).
_ASSIST_STEP_TERMINAL = frozenset({"committed", "skipped", "handed_off", "escalated"})


def _assist_step_progress(step_counts: dict) -> Optional[dict]:
    """Derive a count/pct progress block from an assist step roll-up.

    No time ETA: assist is human-gated between steps, so wall-clock remaining is
    meaningless. Returns None for a trivial (<2 step) session.
    """
    from app.config import settings

    if not settings.progress_eta_enabled:
        return None
    total = sum(int(v) for v in step_counts.values())
    if total < 2:
        return None
    done = sum(int(v) for k, v in step_counts.items() if k in _ASSIST_STEP_TERMINAL)
    pct = int(round(100.0 * done / total)) if total else None
    return {
        "phase": "assisted_executing",
        "label": "Assisted steps",
        "unit": "steps",
        "completed": done,
        "total": total,
        "pct": pct,
        "eta_ms": None,
        "eta_human": None,
        "current_item": None,
        "summary": f"{done}/{total} steps · {pct}%",
        "soft": False,
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
    step_counts = {r["status"]: r["cnt"] for r in rollup}
    return {
        **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in sess_dict.items()},
        "step_counts": step_counts,
        # §17.811 — step progress. Assist is human-gated between steps, so there
        # is no honest wall-clock ETA; report completed/total/pct only.
        "progress": _assist_step_progress(step_counts),
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
    brief = await _post_confirm_brief(db=db, job_id=job_id)

    ctx = await assemble_step_context(
        db=db,
        job_id=job_id,
        node=dict(node_row),
        brief=brief,
        fetch_grounding=None,
    )
    return dict(node_row), ctx


# ── §17.751 — the single session-memory funnel ────────────────────────────


async def _post_confirm_brief(*, db, job_id: str) -> dict:
    """§17.844 — the brief AS CONFIRMED, not as first refined.

    ``research_and_compile`` folds the operator's approval-gate answers into
    ``research_data.brief`` (``user_feedback`` key) but never writes them back
    to ``jobs.refined_brief`` — every assist reader of the stale column was
    structurally blind to the answers (live symptom: the T1 hypervisor
    decision re-asked a question the operator had answered at the gate).
    Prefer the post-confirm copy; fall back to the Phase-1 brief pre-confirm.
    """
    row = (await db.execute(
        text("SELECT refined_brief, research_data FROM jobs WHERE id = :id"),
        {"id": job_id},
    )).mappings().first()
    if not row:
        return {}
    research = row.get("research_data")
    if isinstance(research, str):
        try:
            research = json.loads(research)
        except (ValueError, TypeError):
            research = None
    post = (research or {}).get("brief") if isinstance(research, dict) else None
    if isinstance(post, dict) and post:
        return post
    fallback = row.get("refined_brief")
    if isinstance(fallback, str):
        try:
            fallback = json.loads(fallback)
        except (ValueError, TypeError):
            fallback = None
    return fallback if isinstance(fallback, dict) else {}


def _brief_essentials_block(brief: dict) -> str:
    """§17.844 — the operator-established facts every generation must honor.

    ``build_base_prompt`` renders only ``brief.description``; the inventory
    (``inputs_available``), the constraints, and the approval-gate answers
    never reached assist prompts (live symptoms: guidance blind to the
    hardware list given in the first request; answered questions re-asked).
    Rendered once here and prepended via the ``job_digest`` injection path —
    the §17.753 pattern, zero per-site prompt changes. Capped per section so
    a sprawling brief cannot flood the prompt budget.
    """
    if not brief:
        return ""
    parts: list[str] = []
    desc = (brief.get("description") or "").strip()
    if desc:
        parts.append(f"PROJECT: {desc[:500]}")
    constraints = [str(c) for c in (brief.get("constraints") or []) if str(c).strip()][:8]
    if constraints:
        parts.append("CONSTRAINTS (honor these):\n" + "\n".join(f"- {c[:220]}" for c in constraints))
    inputs = [str(i) for i in (brief.get("inputs_available") or []) if str(i).strip()][:10]
    if inputs:
        parts.append("AVAILABLE HARDWARE / INPUTS (the operator already has these — use them, don't ask again):\n"
                     + "\n".join(f"- {i[:220]}" for i in inputs))
    feedback = (brief.get("user_feedback") or "").strip()
    if feedback:
        parts.append("OPERATOR ANSWERS (already given at the approval gate — treat as decided; "
                     "confirm rather than re-ask, and only reopen one if new evidence contradicts it):\n"
                     + feedback[:1600])
    if not parts:
        return ""
    return "── PROJECT BRIEF (operator-established facts) ──\n" + "\n\n".join(parts)


@dataclass
class GenerationMemory:
    """The session-memory bundle EVERY operator-facing generation site injects.

    Assembled in ONE place (``assemble_generation_memory``) so a new or edited
    site cannot silently go memory-blind — the recurring failure mode the log
    closed one site at a time (§17.650 digest, §17.687 history, §17.720 notes on
    ask, §17.726 transcript rebuild, §17.738 recap, §17.745 notes on fix — each
    titled "the LAST blind injection site"). Fields map to the params the
    ``assist_guide.generate_*`` prompts expect; ``conversation`` already has the
    step recap folded in via ``_with_step_recap``."""
    environment: dict
    verbosity: str
    operator_notes: list[dict]
    job_digest: str  # §17.753 — the distilled whole-project recap is prepended here
    history: list[dict]
    recap: str
    conversation: str
    project_recap: str  # §17.753 — the cross-step "living project recap" (raw)


async def assemble_generation_memory(
    *, session_id: str, nk: str, sess: dict, db,
    ctx: "StepContext | None" = None,
    exclude_tail: str | None = None,
    history: list[dict] | None = None,
    digest_excludes: set[str] | None = None,
    title: str | None = None,
) -> GenerationMemory:
    """§17.751 — assemble the full session-memory bundle for a generation turn.

    The one funnel for the sources generation prompts kept forgetting:
    environment + distilled facts (§17.709), operator notes & additions
    (§17.654), the whole-project completed-work digest (§17.650), the recent
    dialogue — rebuilt from the durable transcript when the client sent none, e.g.
    a cross-chat reconnect (§17.687/726) — and the running step recap (§17.738).
    Every operator-facing generation path (guide / stream / fix / research /
    decision) routes through this, so parity is structural, not per-site
    convention. Fail-soft throughout (each source already degrades to ""/[] on
    error), so the bundle is always safe to thread.

    ``digest_excludes`` defaults to the current node plus its direct upstream (both
    already in ``ctx.assembled_prompt``); pass ``set()`` for a whole-project view
    (the research/ask side-query). ``title`` overrides the recap heading when the
    caller has no ``ctx``.
    """
    from app.modules import assist_guide
    environment = _environment_from_metadata(sess.get("metadata"))
    # §17.757 — cross-component: fold in durable facts learned on SIBLING components
    # of the same umbrella (shared host/network/storage) so a later component isn't
    # blind to what an earlier one established. Deduped against this session's own
    # facts; own facts lead. No-op for a standalone job or when the valve is off.
    sib = await _sibling_facts(job_id=str(sess["job_id"]), db=db)
    if sib:
        own = list(environment.get("facts") or [])
        ownk = {str(f).strip().lower() for f in own}
        environment = {**environment,
                       "facts": own + [f for f in sib if f.strip().lower() not in ownk]}
    verbosity = _verbosity_from_metadata(sess.get("metadata"))
    operator_notes = _coerce_notes(sess.get("notes"))
    if digest_excludes is None:
        digest_excludes = {nk, *(ctx.upstream_outputs.keys() if ctx else ())}
    raw_digest = await _job_digest_for(
        db=db, job_id=str(sess["job_id"]), exclude_node_keys=digest_excludes,
    )
    # §17.753 — lead the project-context section with the distilled whole-project
    # recap (the arc: decisions, remaining, cross-step constraints) so step-N
    # guidance/fix/research isn't limited to raw per-step outputs. Reuses the
    # existing job_digest injection path — no new prompt params at the 5 sites.
    project_recap = await get_project_recap(job_id=str(sess["job_id"]), db=db)
    recap_block = assist_guide.render_project_recap_block(project_recap)
    # §17.844 — brief essentials FIRST: constraints, the operator's hardware
    # inventory, and the approval-gate answers (do-not-re-ask). At step 1 the
    # digest and recap are empty — this block is the only project context.
    # Same valve as the digest (§17.650's job-context concern, default ON).
    from app.config import settings as _settings
    brief_block = ""
    if getattr(_settings, "assist_job_context_enabled", True):
        try:
            brief_block = _brief_essentials_block(
                await _post_confirm_brief(db=db, job_id=str(sess["job_id"]))
            )
        except Exception:  # noqa: BLE001 — funnel sources are fail-soft (§17.751)
            logger.warning("assist_brief_block_failed job_id=%s", sess.get("job_id"))
            brief_block = ""
    job_digest = "\n\n".join(b for b in (brief_block, recap_block, raw_digest) if b).strip()
    history = await _history_or_transcript(
        history=history, session_id=session_id, db=db, exclude_tail=exclude_tail,
    )
    recap = await get_step_recap(
        session_id=session_id, node_key=nk,
        title=title or (ctx.title if ctx else None) or nk, db=db,
    )
    conversation = _with_step_recap(_conversation_block_for(history), recap)
    return GenerationMemory(
        environment=environment, verbosity=verbosity, operator_notes=operator_notes,
        job_digest=job_digest, history=history, recap=recap, conversation=conversation,
        project_recap=project_recap,
    )


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

    node_row, ctx = await _assemble_ctx_for_node(db=db, job_id=job_id, node_key=nk)
    # §17.654 — decision nodes get the one-choice-at-a-time, suggest-don't-decide
    # prompt.
    is_decision = is_decision_node(node_row.get("node_type"))
    # §17.751 — single-funnel session memory (env+facts · whole-project digest ·
    # notes · dialogue+transcript fallback · step recap) so this site can't drift
    # memory-blind. Digest excludes this step + its direct parents (already in
    # ctx.assembled_prompt).
    mem = await assemble_generation_memory(
        session_id=session_id, nk=nk, sess=sess, db=db, ctx=ctx,
        exclude_tail=refine, history=history,
    )

    res = await assist_guide.ensure_guidance(
        session_id=session_id,
        node_key=nk,
        ctx=ctx,
        node_description=node_row.get("description"),
        research=research,
        refine_hint=refine,
        force=force,
        domain=node_row.get("domain"),
        environment=mem.environment,
        verbosity=mem.verbosity,
        job_digest=mem.job_digest,
        operator_notes=mem.operator_notes,
        is_decision=is_decision,
        conversation=mem.conversation,
        db=db,
    )
    # §17.726/§17.812 — record what the engine told the operator. Cached
    # re-presents are captured too (gap 2): a walkthrough generated before the
    # capture valve was on, or re-shown after intervening turns, was otherwise
    # absent from the durable transcript. capture_assistant_reply dedupes
    # against the node's most recent assistant turn, so a back-to-back replay
    # still writes nothing.
    if (res.get("guidance") or "").strip():
        await capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="guide",
            content=res["guidance"], db=db,
        )
    result = {
        "session_id": session_id,
        "job_id": job_id,
        "node_key": nk,
        "title": ctx.title,
        "tool": ctx.tool,
        **res,
    }
    # §17.741 — surface the recap to the operator as a "📍 Where we are" panel
    # above the walkthrough (the non-stream path renders result["status_panel"];
    # the stream path yields it as a leading delta — see below).
    if settings.assist_status_panel_enabled:
        panel = assist_guide.render_status_panel(mem.recap)
        if panel:
            result["status_panel"] = panel
    return result


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

    node_row, ctx = await _assemble_ctx_for_node(db=db, job_id=job_id, node_key=nk)
    is_decision = is_decision_node(node_row.get("node_type"))  # §17.654
    # §17.751 — single-funnel session memory (see assemble_generation_memory).
    mem = await assemble_generation_memory(
        session_id=session_id, nk=nk, sess=sess, db=db, ctx=ctx,
        exclude_tail=refine, history=history,
    )

    # §17.741 — lead with the operator-facing "📍 Where we are" panel, as a delta
    # so it renders ABOVE the streamed walkthrough. Not teed into _buf: the panel
    # is a derived, ephemeral view of the recap, not part of the guidance text
    # captured to the transcript.
    if settings.assist_status_panel_enabled:
        _panel = assist_guide.render_status_panel(mem.recap)
        if _panel:
            yield {"type": "delta", "text": _panel + "\n\n"}

    # §17.726 — tee the streamed walkthrough so the assembled reply lands in the
    # transcript once the stream completes.
    _buf: list[str] = []
    async for ev in assist_guide.generate_guidance_stream(
        session_id=session_id,
        node_key=nk,
        ctx=ctx,
        node_description=node_row.get("description"),
        research=research,
        refine_hint=refine,
        force=force,
        domain=node_row.get("domain"),
        environment=mem.environment,
        verbosity=mem.verbosity,
        job_digest=mem.job_digest,
        operator_notes=mem.operator_notes,
        is_decision=is_decision,
        conversation=mem.conversation,
        db=db,
    ):
        if ev.get("type") == "delta":
            _buf.append(ev.get("text") or "")
        yield ev
    # §17.812 (gap 2) — cached streams are captured too; the in-capture dedupe
    # keeps back-to-back replays out of the transcript.
    if _buf:
        await capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="guide",
            content="".join(_buf), db=db,
        )


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

    if not (question or "").strip():
        raise ValueError("research question is empty")
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, metadata, notes
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
    # §17.751 — same single-funnel session memory as guide/fix. `digest_excludes`
    # is empty here: the ask/research side-query wants the WHOLE-project view
    # (including the current node), not the walkthrough's parents-excluded digest.
    mem = await assemble_generation_memory(
        session_id=session_id, nk=nk or "?", sess=sess, db=db,
        exclude_tail=question, history=history, digest_excludes=set(),
        title=nk or "",
    )
    environment = mem.environment
    context_parts: list[str] = []
    goal = (brief or {}).get("description") or (brief or {}).get("title") or ""
    if isinstance(goal, str) and goal.strip():
        context_parts.append(f"## Project goal\n{goal.strip()}")
    # §17.720 — this path grounded on the bare env block only, so operator NOTES
    # (their decisions/pivots) never reached the answer: a session whose notes
    # said "set up the new Proxmox ISO first" kept getting answers arguing for
    # the brief's in-place plan. Inject the same unified memory (notes + facts,
    # with §17.714 supersession) every other prompt site grounds on.
    context_parts.extend(
        assist_guide._render_memory_or_legacy(mem.environment, mem.operator_notes)
    )
    if mem.job_digest:
        context_parts.append(mem.job_digest)
    conversation = _conversation_block_for(mem.history)  # §17.687
    if conversation:
        context_parts.append(conversation)
    recap_block = assist_guide.render_step_recap_block(mem.recap)  # §17.738
    if recap_block:
        context_parts.append(recap_block)
    job_context = "\n\n".join(context_parts) or None

    res = await assist_guide.research_one(
        question=question, node_key=nk or "?", domain=domain,
        job_context=job_context, context_hint=_kb_hint_from(brief, environment),
    )
    # §17.851b — research how-to answers carry commands too: same
    # code-enforced placeholder resolution as walkthroughs and fixes.
    from app.config import settings as _settings
    if (res.get("answer") or "").strip() and _settings.assist_placeholder_resolver_enabled:
        resolved, _rmap = await assist_guide.resolve_placeholders(
            text=res["answer"], session_id=session_id, environment=environment,
            step_title=nk or "", db=db, node_key=nk,  # §17.892 — scoped auto-pin
        )
        res["answer"] = resolved
    # §17.726 — the answer is what the engine told the operator; record it.
    if (res.get("answer") or "").strip():
        await capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="ask",
            content=res["answer"], db=db,
        )
    return {"session_id": session_id, "node_key": nk, **res}


async def _fix_failure_streak(
    *, session_id: str, node_key: str, db,
) -> tuple[int, str]:
    """§17.881/882 — how many fixes has this step burned without resolving?

    §17.882 counting fix: the first cut counted the LEADING consecutive run of
    kind='fix' assistant turns — so an interleaved Guide press RESET the count
    to zero and the escalation never fired on the live T16 marathon (5 fixes,
    zero escalations). A guide between fixes does not mean the problem changed:
    count EVERY fix turn since the step was claimed (presented_at), and extract
    the fenced commands from ALL of them — each was demonstrably followed by
    the operator returning with another error. Returns
    ``(streak, failed_commands_text)``. Fail-soft → (0, "")."""
    import re as _re
    try:
        # §17.886 (audit #6) — the streak is NODE-scoped, NOT claim-scoped.
        # The previous `created_at >= presented_at` filter was zeroed every
        # time a §17.878/880 claim-repair re-stamped presented_at mid-marathon,
        # silently disarming the whole §17.881-883 enforcement stack. The
        # node_key scope already isolates the step's troubleshooting history;
        # re-claims of the same node are the same problem context.
        rows = (await db.execute(
            text("""
                SELECT content FROM assist_turns
                 WHERE session_id = :sid AND node_key = :nk
                   AND role = 'assistant' AND kind = 'fix'
                 ORDER BY created_at DESC, id DESC LIMIT 12
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().all()
        streak = len(rows)
        cmds: list[str] = []
        for r in rows:
            for block in _re.findall(r"```[a-z]*\n(.*?)```", r.get("content") or "", _re.S):
                b = block.strip()
                if b and b not in cmds:
                    cmds.append(b)
        return streak, "\n\n".join(cmds[:10])
    except Exception as e:  # noqa: BLE001 — escalation is an enhancement, never a blocker
        # §17.882b — WARNING, not debug: a swallowed error here silently
        # disables the whole no-repeat enforcement stack (lived it).
        logger.warning("assist_fix_streak_failed session_id=%s err=%r", session_id, e)
        return 0, ""


async def _reopen_step_mirrored(
    *, db, job_id: str, session_id: str, node_key: str,
    preserve_guidance: bool = False,
) -> None:
    """§17.899/§17.901 — put a completed step back in play, on BOTH tables plus
    the session pointer, in ONE commit (mirror invariant §17.286).

    `preserve_guidance` is the difference between the two callers, and it
    matters more than it looks:

      * False (a §17.899 DENIAL) — the project moved on and the cached
        walkthrough was written against a state that no longer holds, so drop
        it and redraw (§17.894).
      * True (a §17.901 BACK-A-STEP) — nothing about the project changed; the
        operator just mis-clicked. Redrawing here is what makes "redo a step"
        land somewhere unrecognisable: a fresh generation is a DIFFERENT
        walkthrough, so the operator is returned to a step they were halfway
        through and handed unfamiliar instructions. Keep the exact text they
        were working from.
    """
    await db.execute(
        text("UPDATE dag_nodes SET status='pending', output_text=NULL, "
             "completed_at=NULL, updated_at=NOW() "
             "WHERE job_id=:jid AND node_key=:nk"),
        {"jid": job_id, "nk": node_key},
    )
    if preserve_guidance:
        # presented_at is kept too — re-presenting is exactly what we want.
        await db.execute(
            text("UPDATE assist_steps "
                 "SET status='presented', committed_at=NULL, submitted_at=NULL, "
                 "    evidence=NULL, evidence_kind=NULL, updated_at=NOW() "
                 "WHERE session_id=:sid AND node_key=:nk"),
            {"sid": session_id, "nk": node_key},
        )
    else:
        await db.execute(
            text("UPDATE assist_steps "
                 "SET status='pending', committed_at=NULL, submitted_at=NULL, "
                 "    evidence=NULL, evidence_kind=NULL, presented_at=NULL, "
                 "    guidance=NULL, guidance_status='none', "
                 "    guidance_generated_at=NULL, updated_at=NOW() "
                 "WHERE session_id=:sid AND node_key=:nk"),
            {"sid": session_id, "nk": node_key},
        )
    await db.execute(
        text("UPDATE assist_sessions SET current_node_key=:nk, updated_at=NOW() "
             "WHERE id=:sid AND status IN ('active','paused')"),
        {"nk": node_key, "sid": session_id},
    )
    await db.commit()


async def step_back(*, session_id: str, node_key: str | None = None, db) -> dict | None:
    """§17.901 — undo the last completed step and return the operator to it.

    The gap this fills: `✓ Done → next step` is a one-way door. A mis-click
    closed a step the operator had NOT finished, and the only nearby verb —
    `↻ Re-show step` — re-presents the step the pointer has already moved TO,
    which is why "redo a step brings it to a weird place": it shows the NEXT
    step, not the one you meant to get back to.

    Unlike §17.899's denial reopen this is deliberately unbounded by time or
    turn count (an operator may notice the mis-click much later) and it
    PRESERVES the walkthrough, because nothing about the project changed.

    Without `node_key`, targets the most recently committed step. Returns
    ``{node_key, title, was}`` or None when there is nothing to step back to.
    """
    try:
        sess = (await db.execute(
            text("SELECT job_id, status, current_node_key FROM assist_sessions "
                 "WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess or sess["status"] not in ("active", "paused"):
            return None
        job_id = str(sess["job_id"])
        if node_key:
            row = (await db.execute(
                text("SELECT node_key, status FROM assist_steps "
                     "WHERE session_id=:sid AND node_key=:nk"),
                {"sid": session_id, "nk": node_key},
            )).mappings().first()
            if not row or row["status"] not in ("committed", "skipped"):
                return None
            nk = row["node_key"]
            was = row["status"]
        else:
            # Most recent terminal step. Skipped steps count: ⏩ Skip is just as
            # mis-clickable as ✓ Done, and both leave the operator stranded
            # forward of where they meant to be.
            row = (await db.execute(
                text("SELECT node_key, status FROM assist_steps "
                     "WHERE session_id = :sid AND status IN ('committed','skipped') "
                     "ORDER BY COALESCE(committed_at, updated_at) DESC LIMIT 1"),
                {"sid": session_id},
            )).mappings().first()
            if not row:
                return None
            nk, was = row["node_key"], row["status"]

        node = (await db.execute(
            text("SELECT title FROM dag_nodes WHERE job_id=:jid AND node_key=:nk"),
            {"jid": job_id, "nk": nk},
        )).mappings().first()
        # Read the preserved walkthrough BEFORE the reopen and hand it back, so
        # the caller can re-render it WITHOUT going through ensure_guidance.
        #
        # This is not an optimization, it is the whole point. The reopen sets
        # dag_nodes.updated_at = NOW(), which makes §17.894's `replanned` probe
        # (n.node_key = :nk AND n.updated_at > :gen) fire — so a guide call
        # after a step-back would judge the cache stale and REGENERATE, handing
        # the operator a different walkthrough for work they were part-way
        # through. That is precisely the "redo a step lands somewhere weird"
        # behavior this endpoint exists to remove.
        guidance = (await db.execute(
            text("SELECT guidance FROM assist_steps "
                 "WHERE session_id=:sid AND node_key=:nk"),
            {"sid": session_id, "nk": nk},
        )).scalar()
        await _reopen_step_mirrored(
            db=db, job_id=job_id, session_id=session_id, node_key=nk,
            preserve_guidance=True,
        )
        logger.info("assist_step_back session_id=%s node_key=%s was=%s has_guidance=%s",
                    session_id, nk, was, bool(guidance))
        return {"node_key": nk, "title": (node or {}).get("title") or nk,
                "was": was, "guidance": guidance or ""}
    except Exception as e:  # noqa: BLE001 — §17.882b: log LOUD, never trap the turn
        logger.warning("assist_step_back_failed session_id=%s err=%r", session_id, e)
        return None


async def reopen_denied_step(*, session_id: str, message: str, db) -> dict | None:
    """§17.899 — the operator says work the engine closed was not actually done.
    Reopen that step (mirror invariant §17.286) and point the session at it.

    The missing half of §17.890. That change let a completion CLAIM outrank the
    verifier; this handles a claim that was about the wrong thing. Live
    incident: "It worked Ubuntu Server is now downloading!" closed T23 "Install
    PalWorld server" (the claim was about the OS ISO), and 62 seconds later
    "But we have ONLY installed the ubuntu server and have not installed
    anything else" was correctly not-a-claim — and then ignored. T23 stayed
    done, its PalWorld work migrated into T24, and T24 churned for 22 hours
    unable to satisfy its own goal.

    Deterministic and tightly bounded, because reopening is a plan mutation:
      * the message must be an explicit denial (``looks_like_completion_denial``);
      * only the MOST RECENTLY committed step is eligible;
      * it must have been committed within ``assist_denial_reopen_window_s``
        (default 30 min) — a denial hours later is about something else;
      * at most ``assist_denial_reopen_max_turns`` operator turns may have
        happened since that commit (default 3): a correction comes immediately.

    Returns ``{node_key, title, evidence}`` on a reopen, else ``None``. Fail-soft:
    any error logs LOUD (§17.882b) and returns ``None`` so the turn proceeds.
    """
    from app.config import settings
    from app.modules import assist_policy

    if not settings.assist_denial_reopen_enabled:
        return None
    if not assist_policy.looks_like_completion_denial(message or ""):
        return None
    try:
        sess = (await db.execute(
            text("SELECT job_id, status FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess or sess["status"] not in ("active", "paused"):
            return None
        job_id = str(sess["job_id"])
        # The most recently COMMITTED step, and only if it is recent enough.
        row = (await db.execute(
            text("""
                SELECT node_key, committed_at,
                       EXTRACT(EPOCH FROM (NOW() - committed_at)) AS age_s
                  FROM assist_steps
                 WHERE session_id = :sid AND status = 'committed'
                   AND committed_at IS NOT NULL
                 ORDER BY committed_at DESC
                 LIMIT 1
            """),
            {"sid": session_id},
        )).mappings().first()
        if not row:
            return None
        if float(row["age_s"] or 0) > settings.assist_denial_reopen_window_s:
            logger.info(
                "assist_denial_reopen_skipped reason=too_old session_id=%s nk=%s age_s=%.0f",
                session_id, row["node_key"], float(row["age_s"] or 0))
            return None
        nk = row["node_key"]
        turns_since = (await db.execute(
            text("""
                SELECT COUNT(*) FROM assist_turns
                 WHERE session_id = :sid AND role = 'operator'
                   AND kind <> 'submit' AND created_at > :since
            """),
            {"sid": session_id, "since": row["committed_at"]},
        )).scalar() or 0
        # This turn's own message is already captured, so it counts itself.
        if int(turns_since) > settings.assist_denial_reopen_max_turns:
            logger.info(
                "assist_denial_reopen_skipped reason=too_many_turns session_id=%s "
                "nk=%s turns=%d", session_id, nk, int(turns_since))
            return None

        node = (await db.execute(
            text("SELECT title, output_text FROM dag_nodes "
                 "WHERE job_id = :jid AND node_key = :nk"),
            {"jid": job_id, "nk": nk},
        )).mappings().first()

        await _reopen_step_mirrored(
            db=db, job_id=job_id, session_id=session_id, node_key=nk,
            # A denial means the PROJECT moved on, so the cached walkthrough was
            # written against a state that no longer holds (§17.894) — redraw it.
            preserve_guidance=False,
        )
        logger.warning(  # LOUD: a plan mutation from operator words
            "assist_denial_reopen session_id=%s node_key=%s msg=%r",
            session_id, nk, (message or "")[:120])
        return {
            "node_key": nk,
            "title": (node or {}).get("title") or nk,
            "evidence": (node or {}).get("output_text") or "",
        }
    except Exception as e:  # noqa: BLE001 — §17.882b: log LOUD, never trap the turn
        logger.warning("assist_denial_reopen_failed session_id=%s err=%r",
                       session_id, e)
        return None


async def _prescribed_commands(*, session_id: str, node_key: str, db) -> str:
    """§17.898 — what the ENGINE told the operator to run on this step.

    The live incident this exists for: on T24 the engine's own guide (turn
    1319) and ask (turn 1308) prescribed ``pct enter 106`` — a CONTAINER
    command — for a resource its own facts ledger records as ``VM 106``. When
    it failed, the fix turn (1330) opened with *"The error happened because YOU
    used `pct enter`"*. The engine had no record of its own prescriptions, so
    it misattributed its mistake to the operator and could not reason "I never
    asked you to start a container."

    §17.881's ``_fix_failure_streak`` already extracts commands, but only from
    ``kind='fix'`` turns — the two turns that actually issued ``pct enter``
    were a guide and an ask, so they were invisible. This reads EVERY
    assistant turn on the step and labels each command with the turn that
    issued it. Fail-soft → "" (self-attribution is grounding, never a blocker).
    """
    import re as _re
    try:
        rows = (await db.execute(
            text("""
                SELECT kind, content FROM assist_turns
                 WHERE session_id = :sid AND node_key = :nk AND role = 'assistant'
                 ORDER BY created_at DESC, id DESC LIMIT 12
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().all()
        out: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for block in _re.findall(r"```[a-z]*\n(.*?)```", r.get("content") or "", _re.S):
                b = block.strip()
                if b and b not in seen:
                    seen.add(b)
                    out.append(f"[{r.get('kind') or 'reply'}] {b}")
        return "\n\n".join(out[:12])
    except Exception as e:  # noqa: BLE001 — §17.882b: log LOUD, never swallow
        logger.warning("assist_prescribed_commands_failed session_id=%s err=%r",
                       session_id, e)
        return ""


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
            SELECT id, job_id, status, current_node_key, metadata, notes
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

    node_row, ctx = await _assemble_ctx_for_node(db=db, job_id=job_id, node_key=nk)
    # §17.745/751 — /fix is the most-used assist path and was historically the
    # last memory-blind site; it now injects the SAME single-funnel session memory
    # as /guide (env+facts · digest · notes+§17.714 supersession · dialogue+recap),
    # so an explicit pivot or easiest-tool preference always reaches it.
    mem = await assemble_generation_memory(
        session_id=session_id, nk=nk, sess=sess, db=db, ctx=ctx,
        exclude_tail=error, history=history,
    )
    # §17.881 — repeat-failure escalation: consecutive unresolved fixes on this
    # node mean the current METHOD is failing, not just the last command. The
    # streak + the previously-prescribed commands thread into generate_fix,
    # which (at threshold) demands a materially different approach and floors
    # the research query budget.
    streak, failed_cmds = await _fix_failure_streak(
        session_id=session_id, node_key=nk, db=db,
    )
    # §17.898 — the engine's OWN prescriptions on this step, so a fix can never
    # again blame the operator for a command the engine issued.
    prescribed = await _prescribed_commands(session_id=session_id, node_key=nk, db=db)
    res = await assist_guide.generate_fix(
        ctx=ctx,
        error_text=error,
        research=research,
        environment=mem.environment,
        failure_streak=streak,
        failed_commands=failed_cmds,
        prescribed_commands=prescribed,
        node_key=nk,
        domain=node_row.get("domain"),
        verbosity=mem.verbosity,
        job_digest=mem.job_digest,
        operator_notes=mem.operator_notes,  # §17.745 — notes + reset supersession
        conversation=mem.conversation,  # §17.687 + §17.738 recap
    )
    # §17.851b — fix commands get the same code-enforced placeholder
    # resolution as walkthroughs (carry-through: every operator-facing
    # command surface, not just /guide).
    from app.config import settings as _settings
    if (res.get("fix") or "").strip() and _settings.assist_placeholder_resolver_enabled:
        resolved, _rmap = await assist_guide.resolve_placeholders(
            text=res["fix"], session_id=session_id, environment=mem.environment,
            step_title=ctx.title, db=db, node_key=node_key,  # §17.892
        )
        res["fix"] = resolved
    # §17.726 — record the corrective steps the engine gave the operator.
    if (res.get("fix") or "").strip():
        await capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="fix",
            content=res["fix"], db=db,
        )
    # Capture the blocker on the friction trail (best-effort).
    try:
        await record_friction(
            session_id=session_id, node_key=nk,
            note=f"hit error: {error.strip()[:200]}", db=db,
        )
    except Exception as exc:  # never fail the fix on a friction-log hiccup
        logger.warning("assist_fix_friction_record_failed: %s", exc)
    out = {"session_id": session_id, "node_key": nk, "title": ctx.title, **res}
    # §17.741 — surface the "📍 Where we are" panel above the fix too, so a long
    # troubleshooting marathon stays oriented for the operator, not just the model.
    if settings.assist_status_panel_enabled:
        panel = assist_guide.render_status_panel(mem.recap)
        if panel:
            out["status_panel"] = panel
    return out


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
    # §17.771 (Phase 1) — SHADOW: run the unified decision on this real turn and
    # log how it compares to the classifier the pipeline actually uses. Fire-and-
    # forget with its own DB session; no-op unless the valve is on; never blocks
    # or alters the live turn.
    try:
        from app.modules import assist_decide
        assist_decide.fire_shadow_decision(
            session_id=session_id, message=message, node_key=nk,
            history=history, classifier_intent=res.get("intent") or "question",
        )
    except Exception:  # shadow must never surface into the live turn
        logger.debug("assist shadow fire skipped", exc_info=True)
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
    *, db, limit: int = 25, in_progress: bool = False, owner: str | None = None,
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
    # §17.810 — scope to the caller's own jobs when an owner is supplied (a
    # non-admin principal); None (admin / single-user) means no restriction.
    owner_clause = " AND j.owner = :owner" if owner is not None else ""
    # §17.721 — thread the live session's last_activity_at through so the
    # pipeline's continuity reconnect can tell "the session the operator is
    # mid-conversation in" apart from a title-similar sibling. Only an
    # active/paused session counts as live activity.
    rows = (await db.execute(
        text(f"""
            SELECT j.id, j.title, j.status, j.job_type,
                   (SELECT COUNT(*) FROM dag_nodes n WHERE n.job_id = j.id) AS node_count,
                   s.last_activity_at
              FROM jobs j
              LEFT JOIN assist_sessions s
                ON s.job_id = j.id AND s.status IN ('active', 'paused')
             WHERE j.status = ANY(:statuses){owner_clause}
             ORDER BY j.created_at DESC
             LIMIT :lim
        """),
        {"statuses": list(statuses), "lim": limit,
         **({"owner": owner} if owner is not None else {})},
    )).mappings().all()
    out: list[dict] = []
    for r in rows:
        # Umbrella jobs assist via their components, not directly; skip 0-node.
        if (r.get("job_type") or "") == "umbrella":
            continue
        if not r.get("node_count"):
            continue
        la = r.get("last_activity_at")
        out.append({
            "job_id": str(r["id"]),
            "title": r["title"],
            "status": r["status"],
            "node_count": int(r["node_count"]),
            "last_activity_at": la.isoformat() if la is not None else None,
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


# §17.856 — the environment concern (accessors + exec-context monitor) moved to
# app/modules/assist_environment.py; re-exported so assist_agent.<NAME>, the wide
# internal use of _environment_from_metadata/_verbosity_from_metadata, and the
# tests keep resolving.
from app.modules.assist_environment import (  # noqa: F401,E402
    _environment_from_metadata,
    _VERBOSITY_LEVELS,
    _verbosity_from_metadata,
    get_environment,
    set_environment,
    _SHELL_PROMPT_RE,
    _EXEC_CTX_SENTINEL,
    _detect_shell_context,
    _exec_context_profile,
    _CTX_USER_RE,
    _CTX_HOST_RE,
    _apply_shell_context,
    capture_execution_context,
)

# §17.856 — transcript helpers moved to app/modules/assist_turns.py;
# re-exported so assist_agent.<NAME> keeps resolving.
from app.modules.assist_turns import (  # noqa: F401,E402
    _conversation_block_for,
    _with_step_recap,
    ingest_turn,
    capture_assistant_reply,
    history_from_turns,
    _history_or_transcript,
    list_turns,
    _render_node_transcript,
)

# §17.856 — handoff moved to app/modules/assist_handoff.py; re-exported.
from app.modules.assist_handoff import (  # noqa: F401,E402
    _sse,
    handoff_step,
    spawn_handoff_background,
    _HANDOFF_BACKGROUND_TASKS,
)

# §17.856 — memory/facts moved to app/modules/assist_memory.py; re-exported
# (incl. _NOTE_KINDS, used by the staying record_note).
from app.modules.assist_memory import (  # noqa: F401,E402
    _norm_note,
    _derived_recently,
    derive_turn_memory,
    _derive_turn_memory_bg,
    schedule_derive_turn_memory,
    drain_derive_tasks,
    _fact_count_of,
    _apply_fact_merges,
    consolidate_session_facts,
    _consolidate_facts_bg,
    schedule_consolidate_facts,
    drain_consolidate_tasks,
    capture_session_facts,
    learn_from_submit,
    reconcile_on_commit,
    schedule_reconcile_on_commit,
    sweep_superseded_facts,
    _sibling_facts,
    _durable_facts_for_session,
    check_submit_grounding,
    _DERIVE_TASKS,
    _TRIVIAL_TURN,
    _RECENT_DERIVES,
    _RECENT_DERIVE_TTL,
    _CONSOLIDATE_TASKS,
    _CONSOLIDATE_REGROW,
    _NOTE_KINDS,
)

# §17.856 — notes/replan/friction moved to app/modules/assist_notes.py; re-exported.
from app.modules.assist_notes import (  # noqa: F401,E402
    record_friction,
    list_friction,
    record_note,
    add_step,
    assess_note_impact,
    _stage_replan_proposal,
    detect_reroute,
    _pending_replan_from_metadata,
    _discarded_replans_from_metadata,
    _replan_signature,
    get_pending_replan,
    apply_pending_replan,
    _coerce_notes,
    list_notes,
)


def _note_impact_facts_block(metadata: Any) -> str:
    """§17.752 — the observed-facts block for the note-impact / pivot analyzer,
    gated by ``assist_note_impact_facts_aware``. "" when off or no facts, so
    callers thread it unconditionally."""
    from app.config import settings
    if not settings.assist_note_impact_facts_aware:
        return ""
    from app.modules import assist_guide
    return assist_guide.render_facts_block(_environment_from_metadata(metadata))






async def _note_impact_project_block(job_id: str, db) -> str:
    """§17.753 — the distilled whole-project recap block for the note/pivot
    analyzer, so it judges impact against the arc (what's already built/decided),
    not just the pending list. "" when the project recap is disabled/empty."""
    from app.modules import assist_guide
    return assist_guide.render_project_recap_block(
        await get_project_recap(job_id=job_id, db=db))








# ── Unconditional per-turn derive (§17.715 — review + log EVERY message) ────


















# ── Ledger consolidation (§17.727 — merge redundant same-truth facts) ───────
















# ── Unified session memory (§17.710a — lossless raw capture) ──────────────












# ── Per-step progress recap (§17.738 — coherence over a long step) ──────────




async def get_step_recap(
    *, session_id: str, node_key: str | None, title: str = "", db,
) -> str:
    """§17.738 — return the running progress recap for a step, refreshing it from
    the full node-scoped transcript when it has grown by
    ``assist_step_recap_every`` turns since the last recap (else return the
    cached one). Gated on ``assist_step_recap_enabled``; fail-soft → "" so every
    caller can thread it unconditionally.

    This is what keeps fix/guide/research coherent over a long troubleshooting
    step: the 6-turn window (§17.687) loses the thread; this recap is distilled
    from the WHOLE step transcript (DB-backed, survives restarts) and cached."""
    from app.config import settings
    from app.modules import assist_guide

    # §17.741 — the operator-facing "📍 Where we are" panel is a presentation of
    # this recap, so enabling the panel forces the recap to be computed even when
    # the internal-only recap flag is off.
    if not ((settings.assist_step_recap_enabled or settings.assist_status_panel_enabled) and node_key):
        return ""
    try:
        step = (await db.execute(
            text("""
                SELECT progress_recap, progress_recap_turns
                  FROM assist_steps WHERE session_id = :sid AND node_key = :nk
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().first()
        if not step:
            return ""
        turns = (await db.execute(
            text("""
                SELECT role, content FROM assist_turns
                 WHERE session_id = :sid AND node_key = :nk AND kind <> 'skip'
                 ORDER BY created_at, id
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().all()
        n = len(turns)
        cached = (step.get("progress_recap") or "").strip()
        watermark = int(step.get("progress_recap_turns") or 0)
        # Not enough history yet, or not grown enough since the last recap → use
        # the cache (which may be "").
        if n < int(settings.assist_step_recap_min_turns):
            return cached
        if cached and n < watermark + int(settings.assist_step_recap_every):
            return cached
        transcript = _render_node_transcript([dict(t) for t in turns])
        # §17.752 — ground the recap in the durable ledgers too, not just this
        # node's transcript: a constraint the operator stated on an earlier step,
        # or a distilled system fact (§17.709), belongs in CONSTRAINTS/CONTEXT even
        # if it wasn't re-said here. DONE/OPEN/NEXT stay transcript-derived.
        facts_block = notes_block = ""
        if settings.assist_recap_ledger_aware:
            srow = (await db.execute(
                text("SELECT notes, metadata FROM assist_sessions WHERE id = :sid"),
                {"sid": session_id},
            )).mappings().first()
            if srow:
                facts_block = assist_guide.render_facts_block(
                    _environment_from_metadata(srow.get("metadata")))
                notes_block = assist_guide.render_operator_notes_block(
                    _coerce_notes(srow.get("notes")))
        recap = await assist_guide.summarize_step_progress(
            title=title or node_key, transcript=transcript,
            facts_block=facts_block, notes_block=notes_block,
        )
        if not recap:
            return cached
        # §17.812 — persist the cache on its OWN session: this helper runs
        # mid-request inside arbitrary callers (guide/fix/track/note-impact),
        # and committing the CALLER's transaction here silently flushed
        # whatever half-done work the caller had pending — a commit point the
        # caller never chose. The cache write is independent, so isolate it.
        async with async_session() as cache_db:
            await cache_db.execute(
                text("""
                    UPDATE assist_steps
                       SET progress_recap = :r, progress_recap_turns = :n, updated_at = NOW()
                     WHERE session_id = :sid AND node_key = :nk
                """),
                {"r": recap, "n": n, "sid": session_id, "nk": node_key},
            )
            await cache_db.commit()
        logger.info(
            "assist_step_recap_refreshed session_id=%s node_key=%s turns=%d",
            session_id, node_key, n,
        )
        return recap
    except Exception as e:  # noqa: BLE001 — recap must never break the turn
        logger.debug("get_step_recap_failed session_id=%s err=%r", session_id, e)
        return ""


async def get_project_recap(*, job_id: str, db) -> str:
    """§17.753 — the cross-step "living project recap" (§17.679): a distilled,
    cached, EVOLVING whole-project state board (goal · done phases · in-progress ·
    remaining · decisions · constraints · system facts), refreshed only when the
    count of DONE nodes grows past the watermark since the last recap — so it costs
    ~one LLM call per completed step and is cached across the many turns within a
    step. Gated on ``assist_project_recap_enabled``; fail-soft → "" so every caller
    (the §17.751 funnel, the note/pivot analyzer) can thread it unconditionally.

    Unlike ``assemble_job_digest`` (§17.650, which dumps raw done-node outputs), this
    is a distilled arc: what earlier steps DECIDED, what remains, and the
    project-wide constraints/system facts — the piece step-N guidance/pivot was
    blind to."""
    from app.config import settings
    from app.modules import assist_guide

    if not settings.assist_project_recap_enabled:
        return ""
    try:
        job = (await db.execute(
            text("SELECT refined_brief, project_recap, project_recap_nodes "
                 "FROM jobs WHERE id = :jid"),
            {"jid": job_id},
        )).mappings().first()
        if not job:
            return ""
        nodes = (await db.execute(
            text("""
                SELECT node_key, title, status, output_text, execution_order
                  FROM dag_nodes WHERE job_id = :jid
                 ORDER BY execution_order NULLS LAST, node_key
            """),
            {"jid": job_id},
        )).mappings().all()
        if not nodes:
            return ""
        done_n = sum(1 for n in nodes if n["status"] == "done")
        cached = (job.get("project_recap") or "").strip()
        watermark = int(job.get("project_recap_nodes") or 0)
        # Nothing meaningfully done yet, or no new completions since the last recap
        # → reuse the cache (which may be "").
        if done_n < int(settings.assist_project_recap_min_nodes):
            return cached
        if cached and done_n < watermark + int(settings.assist_project_recap_every):
            return cached
        # Distill the arc from step statuses + a short preview of each DONE output.
        lines: list[str] = []
        for n in nodes:
            head = f"- {n['node_key']} ({n['status']}): {n['title'] or n['node_key']}"
            if n["status"] == "done" and (n["output_text"] or "").strip():
                head += f" — produced: {n['output_text'].strip()[:300]}"
            lines.append(head)
        nodes_block = "\n".join(lines)
        # Cross-step ledgers live on the job's assist session (if any).
        facts_block = notes_block = ""
        srow = (await db.execute(
            text("SELECT notes, metadata FROM assist_sessions WHERE job_id = :jid"),
            {"jid": job_id},
        )).mappings().first()
        if srow:
            facts_block = assist_guide.render_facts_block(
                _environment_from_metadata(srow.get("metadata")))
            notes_block = assist_guide.render_operator_notes_block(
                _coerce_notes(srow.get("notes")))
        brief = job.get("refined_brief") or {}
        if isinstance(brief, str):
            try:
                brief = json.loads(brief)
            except (ValueError, TypeError):
                brief = {}
        goal = (brief.get("description") or brief.get("title") or "") if isinstance(brief, dict) else ""
        recap = await assist_guide.summarize_project_progress(
            goal=goal, nodes_block=nodes_block,
            facts_block=facts_block, notes_block=notes_block,
        )
        if not recap:
            return cached
        # §17.812 — own-session cache write, same rationale as get_step_recap:
        # never commit the caller's transaction mid-request.
        async with async_session() as cache_db:
            await cache_db.execute(
                text("UPDATE jobs SET project_recap = :r, project_recap_nodes = :n "
                     "WHERE id = :jid"),
                {"r": recap, "n": done_n, "jid": job_id},
            )
            await cache_db.commit()
        logger.info("assist_project_recap_refreshed job_id=%s done_nodes=%d", job_id, done_n)
        return recap
    except Exception as e:  # noqa: BLE001 — a recap must never break the turn
        logger.debug("get_project_recap_failed job_id=%s err=%r", job_id, e)
        return ""


async def build_reconnect_orientation(*, session_id: str, db) -> dict | None:
    """§17.761 — a compact WHERE-YOU-ARE orientation for the reconnect/start path:
    when the operator picks a job back up (a fresh chat, `/assist <job>`), the
    engine dropped them straight into a step with no sense of the whole project.
    This returns a deterministic snapshot — job title, progress, recently-done
    steps, the current step, and what's next — plus the CACHED §17.753 project
    recap (read-only; NO model call on the start path). Valve-gated; fail-soft to
    ``None`` (caller just omits the orientation)."""
    from app.config import settings
    if not settings.assist_reconnect_orientation_enabled:
        return None
    try:
        sess = (await db.execute(
            text("SELECT job_id, current_node_key FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess:
            return None
        job_id = str(sess["job_id"])
        job = (await db.execute(
            text("SELECT title, project_recap FROM jobs WHERE id = :jid"),
            {"jid": job_id},
        )).mappings().first()
        nodes = (await db.execute(
            text("SELECT node_key, title, status FROM dag_nodes WHERE job_id = :jid "
                 "ORDER BY execution_order NULLS LAST, node_key"),
            {"jid": job_id},
        )).mappings().all()
        if not nodes:
            return None
        cur_key = sess["current_node_key"]
        done = [n for n in nodes if n["status"] in ("done", "skipped")]
        pending = [n for n in nodes if n["status"] == "pending"]
        cur = next((n for n in nodes if n["node_key"] == cur_key), None)
        upcoming = [n["title"] for n in pending if n["node_key"] != cur_key][:3]
        return {
            "job_title": (job or {}).get("title") or "this build",
            "done_n": len(done),
            "total_n": len(nodes),
            "current_title": (cur or {}).get("title"),
            "current_key": cur_key,
            "done_recent": [n["title"] for n in done][-3:],
            "upcoming": upcoming,
            "project_recap": ((job or {}).get("project_recap") or "").strip() or None,
        }
    except Exception as e:  # noqa: BLE001 — orientation must never break start
        logger.debug("build_reconnect_orientation_failed session_id=%s err=%r", session_id, e)
        return None


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
        is_decision=is_decision_node(row.get("node_type")),
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


def is_decision_node(node_type: str | None) -> bool:
    """§17.771 (deferred, now done) — the SINGLE source of truth for "is this a
    decision node". This was a bare ``(node_type or "").lower() == "decision"``
    repeated at ~5 sites; a typo/case-slip at any one silently downgraded a
    decision to the committal noncode guide path (which "states the recommended
    choice, do not leave it hanging" — the opposite posture). Route every
    assist-side check through here so the literal lives in ONE place."""
    return (node_type or "").strip().lower() == "decision"


def _collect_step_kind(node_type: str | None, task_prompt: str) -> Optional[str]:
    """§17.689/§17.690 — classify a step as a COLLECT step whose deliverable the
    operator supplies across one or more turns. Returns ``'decision'`` (a choice
    / concrete artifact), ``'gather'`` (specific requested information), or
    ``None`` (not a collect step — commit normally)."""
    if is_decision_node(node_type):
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
    # §17.751 — parity with the primary generators (guide/fix/research) via the
    # single memory funnel: rebuild the dialogue from the durable transcript when
    # the client sent none (curl / cross-chat reconnect) and fold in the step
    # recap, so a long multi-turn decision keeps the thread instead of
    # deliberating history-blind, and grounds on env/facts/notes/digest.
    mem = await assemble_generation_memory(
        session_id=session_id, nk=node_key, sess=row, db=db,
        exclude_tail=message, history=history,
        digest_excludes={node_key}, title=row["title"] or node_key,
    )
    from app.modules import assist_guide
    # §17.771 (Phase 3) — evidence-backed commit for a DECISION: research the
    # operator's ACTUAL system so the recommendation is current + system-specific
    # instead of drawn from stale model memory (the audit's "commit path has zero
    # fresh research" gap). Gather steps just collect operator input — no research.
    # Fail-soft, bounded by assist_guide_max_research_queries (0 → skip).
    research_block = ""
    if kind == "decision" and settings.assist_guide_max_research_queries > 0:
        try:
            sources = await assist_guide._research_prepass(
                task_text=task_prompt, tool="LLM",
                role=settings.assist_guide_model_role,
                max_queries=settings.assist_guide_max_research_queries,
                node_key=node_key, domain=None,
                environment_block=assist_guide.render_environment_block(mem.environment),
            )
            research_block = assist_guide._render_research_block(sources)
        except Exception as exc:  # research must never trap the commit
            logger.warning("assist_decision_research_failed node=%s err=%r", node_key, exc)
    res = await assist_guide.deliberate_decision(
        title=row["title"] or node_key,
        task_prompt=task_prompt,
        environment=mem.environment,
        job_digest=mem.job_digest,
        operator_notes=mem.operator_notes,
        conversation=mem.conversation,
        latest_message=message,
        kind=kind,
        research_block=research_block,
    )
    status = res.get("status")
    if status == "needs_input" and (res.get("message") or "").strip():
        # §17.726 — the deliberation reply is what the engine told the operator.
        await capture_assistant_reply(
            session_id=session_id, node_key=node_key, kind="deliberation",
            content=res["message"].strip(), db=db,
        )
        return {"status": "needs_input", "message": res["message"].strip(),
                "collect_kind": kind}
    if status == "resolved" and (res.get("decision_record") or "").strip():
        await capture_assistant_reply(
            session_id=session_id, node_key=node_key, kind="deliberation",
            content=(res.get("message") or res["decision_record"]).strip(), db=db,
        )
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
    skip_divergence_replan: bool = False,
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
    # §17.771 — skip the divergence re-plan when the endpoint will run the
    # dedicated constraint-adaptation instead (a goal-met-via-alternative commit):
    # the two would stage conflicting proposals (divergence says "your output
    # differs from the plan", adaptation says "the plan's method was impossible —
    # here's the revision"). The adaptation path is the deliberate, reliable one.
    if action == "submit" and not verdict_failed and not skip_divergence_replan:
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
        is_decision=is_decision_node(node["node_type"]),
    )


async def adapt_step_to_constraint(
    *, session_id: str, node_key: str, constraint: str, db,
) -> dict | None:
    """§17.771 — a step committed via a valid ALTERNATIVE because a hardware/
    software constraint ruled out the planned method. ADAPT THE PLAN TO REALITY
    (not just wave the step through):

    1. Record the constraint as a durable ``constraint`` note so it feeds forward
       into EVERY later step's guidance (via the memory funnel) — no downstream
       step re-proposes the ruled-out method, and later generation grounds on the
       real capability of this system.
    2. Run the note-impact analyzer over the PENDING steps that ASSUMED the
       impossible method, and stage a surface-and-ask re-plan for any it finds
       (revise/drop) so the plan matches reality instead of lying about it.

    Returns ``{constraint, affected}`` for the caller to surface, or None when
    there's nothing to adapt. Fail-soft throughout — never breaks the commit.
    """
    from app.config import settings
    constraint = (constraint or "").strip()
    if not constraint:
        return None
    sess = (await db.execute(
        text("SELECT job_id, status, metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess or sess["status"] not in ("active", "paused"):
        return None
    job_id = str(sess["job_id"])
    # (1) durable constraint note — propagates to every later step's guidance.
    try:
        await record_note(
            session_id=session_id, text_=constraint, kind="constraint",
            node_key=node_key, db=db,
        )
    except Exception as e:  # noqa: BLE001 — adaptation must never break the commit
        logger.warning("adapt_constraint_note_failed session_id=%s err=%r", session_id, e)
    out = {"constraint": constraint, "affected": []}
    # (2) revise the PENDING steps that assumed the ruled-out method.
    if not settings.assist_note_replan_enabled:
        return out
    from app.modules import assist_replan
    try:
        impact = await assist_replan.analyze_note_impact(
            db=db, job_id=job_id, note_text=constraint, note_kind="constraint",
            strict=False,  # an explicit, confirmed constraint — flag borderline steps
            facts_block=_note_impact_facts_block(sess.get("metadata")),
            project_recap_block=await _note_impact_project_block(job_id, db),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("adapt_constraint_analyze_failed session_id=%s err=%r", session_id, e)
        return out
    affected = impact.get("affected") or []
    if affected:
        try:
            await _stage_replan_proposal(
                session_id=session_id, note_text=constraint,
                note_kind="constraint", affected=affected, db=db,
            )
            out["affected"] = affected
        except Exception as e:  # noqa: BLE001
            logger.warning("adapt_constraint_stage_failed session_id=%s err=%r", session_id, e)
    return out


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






# ── Notes & additions (§17.654 — capture what the operator raises mid-flow) ─





























# ── Handoff (assist -> autonomous executor for one node or all remaining) ─








