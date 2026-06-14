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
    presented steps are in flight — otherwise it just means the user
    has work to submit.

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
            RETURNING id, node_key, guidance_status
        """),
        {"sid": session_id},
    )).mappings().first()
    if not claimed:
        await db.commit()
        # §17.512 — no pending step is claimable. Before reporting "nothing to
        # do", re-surface a step already PRESENTED to this user but not yet
        # submitted, so a lost / scrolled-away / reconnect walkthrough is
        # recoverable via `/assist next` instead of being a dead-end. Only
        # fires when nothing new is claimable, so it never blocks forward
        # progress on parallel/ready steps.
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
                   tool, domain, execution_order
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
            SELECT id, job_id, status, current_node_key, metadata
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise ValueError(f"session status {sess['status']!r} cannot generate guidance")
    job_id = str(sess["job_id"])
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
            SELECT id, job_id, status, current_node_key, metadata
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise ValueError(f"session status {sess['status']!r} cannot generate guidance")
    job_id = str(sess["job_id"])
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
    domain (when a node is in scope) to bias Milvus retrieval.
    """
    from app.modules import assist_guide

    if not (question or "").strip():
        raise ValueError("research question is empty")
    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    nk = node_key or sess["current_node_key"]
    domain = None
    if nk:
        drow = (await db.execute(
            text("SELECT domain FROM dag_nodes WHERE job_id = :jid AND node_key = :nk"),
            {"jid": str(sess["job_id"]), "nk": nk},
        )).mappings().first()
        domain = (drow or {}).get("domain")
    res = await assist_guide.research_one(
        question=question, node_key=nk or "?", domain=domain,
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

    res = await assist_guide.generate_fix(
        ctx=ctx,
        error_text=error,
        research=research,
        environment=environment,
        node_key=nk,
        domain=node_row.get("domain"),
        verbosity=verbosity,
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


# ── Environment capture (§17.487 — concrete commands, not placeholders) ────


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
    from app.modules.execution_agent import execute_all_nodes

    try:
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

    yield _sse("assist_handoff_done", {
        "session_id": session_id,
        "node_key": node_key,
        "mode": mode,
    })


def _sse(event_type: str, payload: dict) -> str:
    """SSE wire format. Same shape as research_agent / execution_agent."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
