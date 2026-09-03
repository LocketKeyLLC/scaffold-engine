"""Assist operator notes + re-plan + friction — extracted from assist_agent.py.

§17.856 (audit "assist decomposition") — record operator notes/decisions
(record_note, _coerce_notes, list_notes), turn a foundational gap into a step
(add_step), assess a note's plan impact and stage/surface/apply a re-plan
(assess_note_impact / _stage_replan_proposal / get_pending_replan /
apply_pending_replan + the _*_from_metadata / _replan_signature helpers), decide a
fuzzy reroute (detect_reroute, model_general), and the friction log (record_friction
/ list_friction). Calls assist_guide + assist_replan (module imports) and — for
names re-exported through assist_agent (sweep_superseded_facts, the _note_impact_*
blocks, _NOTE_KINDS) — patch-safe function-local imports. Every name re-exported
from assist_agent so assist_agent.<NAME> + the external surface keep resolving.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.modules import assist_guide
from app.modules import assist_replan

logger = logging.getLogger("scaffold.assist")


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


async def record_note(
    *, session_id: str, text_: str, kind: str = "note",
    node_key: str | None = None, dedupe: bool = False, db,
) -> dict | None:
    """§17.654 — append a session-level note/addition. Project-scoped (not tied
    to a step's lifecycle like friction): a new requirement or constraint the
    operator raises should outlive the step it came up on and feed forward into
    every later step's guidance. Appends to ``assist_sessions.notes`` (JSONB
    array). Returns the stored note dict, or None on empty text / missing
    session. Single-statement append; the whole array is re-read cheaply.

    §17.854 (audit C4) — ``dedupe`` skips the append if an identical (kind,text)
    note already exists. Used by the pivot path (detect_reroute), where a
    re-sent already-dismissed pivot message otherwise re-recorded the SAME
    decision note every turn — and every note thereafter rides every prompt."""
    from app.modules.assist_agent import _NOTE_KINDS  # §17.856 re-exports (patch-safe deferred)
    note_text = (text_ or "").strip()
    if not note_text:
        return None
    k = kind if kind in _NOTE_KINDS else "note"
    if dedupe:
        existing = (await db.execute(
            text("""
                SELECT 1 FROM assist_sessions,
                     jsonb_array_elements(COALESCE(notes, '[]'::jsonb)) AS n
                 WHERE id = :sid AND n->>'text' = :txt AND n->>'kind' = :kind
                 LIMIT 1
            """),
            {"sid": session_id, "txt": note_text, "kind": k},
        )).first()
        if existing is not None:
            return {"kind": k, "node_key": node_key, "text": note_text, "deduped": True}
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


async def add_step(
    *, session_id: str, request: str, before_node_key: str | None = None, db,
) -> dict:
    """§17.736 — insert a new guided step the plan doesn't cover, to run BEFORE
    the current blocked step, and point the session at it.

    The reported gap: the operator hit a foundational task the plan never had a
    step for (get the VM connected to the internet), so it was handled as
    scattered one-off `fix`es with no throughline. Re-plan could drop/revise but
    not ADD. This drafts a concrete step (model_general, project-grounded),
    inserts a `dag_nodes` + `assist_steps` row, makes the blocked step DEPEND on
    it (so it's sequenced first via the normal dep gate), resets the blocked
    step from presented→pending, and sets ``current_node_key`` to the new node.
    The new step is then guided by the ordinary walkthrough machinery — the
    gather→paste→verify (§17.731) loop the operator asked for. Fail-soft only on
    the draft; the insert itself raises ValueError on a bad session so the
    router maps it to a 4xx.
    """
    from app.modules.assist_agent import _environment_from_metadata  # §17.856 re-exports (patch-safe deferred)
    from app.modules import assist_guide

    sess = (await db.execute(
        text("""
            SELECT job_id, status, current_node_key, metadata, notes
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess:
        raise ValueError(f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise ValueError(f"session status {sess['status']!r} cannot add a step")
    job_id = str(sess["job_id"])
    before = before_node_key or sess["current_node_key"]

    # Anchor node: the step this new one runs before (its deps + order + tool are
    # inherited so the new node is claimable now and the anchor waits on it).
    anchor = None
    if before:
        anchor = (await db.execute(
            text("""
                SELECT node_key, depends_on, execution_order, tool, domain
                  FROM dag_nodes WHERE job_id = :jid AND node_key = :nk
            """),
            {"jid": job_id, "nk": before},
        )).mappings().first()

    # Draft the step, grounded in the project's brief + memory.
    brief_row = (await db.execute(
        text("SELECT refined_brief FROM jobs WHERE id = :id"), {"id": job_id},
    )).mappings().first()
    brief = (brief_row or {}).get("refined_brief") or {}
    environment = _environment_from_metadata(sess.get("metadata"))
    ctx_parts: list[str] = []
    goal = (brief or {}).get("description") or (brief or {}).get("title") or ""
    if isinstance(goal, str) and goal.strip():
        ctx_parts.append(f"## Project goal\n{goal.strip()}")
    ctx_parts.extend(assist_guide._render_memory_or_legacy(environment, None))
    drafted = await assist_guide.draft_step(
        request=request, job_context="\n\n".join(ctx_parts) or None,
    )

    # Unique node_key: ADD1, ADD2, … (distinct from the T-series; sorts before
    # T* so a same-order tie still serves it first, though the dep gate is what
    # actually sequences it).
    existing = {
        r["node_key"] for r in (await db.execute(
            text("SELECT node_key FROM dag_nodes WHERE job_id = :jid"), {"jid": job_id},
        )).mappings().all()
    }
    n = 1
    while f"ADD{n}" in existing:
        n += 1
    new_key = f"ADD{n}"

    new_deps = list((anchor or {}).get("depends_on") or [])
    order = (anchor or {}).get("execution_order")
    tool = (anchor or {}).get("tool") or "shell"
    domain = (anchor or {}).get("domain")

    await db.execute(
        text("""
            INSERT INTO dag_nodes
                (job_id, node_key, title, description, node_type, status,
                 depends_on, prompt_template, tool, domain, execution_order)
            VALUES (:jid, :nk, :title, :desc, 'task', 'pending',
                    :deps, :prompt, :tool, :domain, :order)
        """),
        {"jid": job_id, "nk": new_key, "title": drafted["title"],
         "desc": drafted["description"], "deps": new_deps,
         "prompt": drafted["description"], "tool": tool, "domain": domain,
         "order": order},
    )
    await db.execute(
        text("""
            INSERT INTO assist_steps (session_id, job_id, node_key, status)
            VALUES (:sid, :jid, :nk, 'pending')
        """),
        {"sid": session_id, "jid": job_id, "nk": new_key},
    )
    # Sequence: the anchor now depends on the new node, and is reset to pending
    # so get_next_step serves the new node first (not the re-presented anchor).
    if anchor:
        await db.execute(
            text("""
                UPDATE dag_nodes
                   SET depends_on = array_append(coalesce(depends_on,'{}'), :nk),
                       updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :anchor
            """),
            {"jid": job_id, "nk": new_key, "anchor": before},
        )
        # §17.911 — REOPEN the anchor, on BOTH tables (mirror invariant §17.286).
        #
        # The first cut reset `assist_steps` only, and only from 'presented'.
        # Live (session 613dd1df): T23 "Install PalWorld server" was recorded
        # `done`/`handed_off` for work that never happened — the operator had
        # spent three days failing to install the OS underneath it (§17.910).
        # Inserting the missing prerequisite left T23 `done`, so once the new
        # step completed T23 would never be presented and the PalWorld install
        # would simply be skipped, while T24 stayed claimable behind a step that
        # was never really finished.
        #
        # A step cannot be both "done" and "blocked on a new prerequisite". The
        # operator asking to insert work BEFORE it is an explicit instruction,
        # not an inference, so reopening is the necessary consequence rather
        # than a §17.891 auto-mutation. Guidance is dropped because the plan
        # itself changed — the cached walkthrough was written against a state
        # that no longer holds (§17.894/§17.899).
        await db.execute(
            text("""
                UPDATE dag_nodes
                   SET status = 'pending', output_text = NULL,
                       completed_at = NULL, updated_at = NOW()
                 WHERE job_id = :jid AND node_key = :anchor
                   AND status IN ('done', 'skipped', 'failed')
            """),
            {"jid": job_id, "anchor": before},
        )
        await db.execute(
            text("""
                UPDATE assist_steps
                   SET status = 'pending', committed_at = NULL, submitted_at = NULL,
                       evidence = NULL, evidence_kind = NULL, presented_at = NULL,
                       guidance = NULL, guidance_status = 'none',
                       guidance_generated_at = NULL, updated_at = NOW()
                 WHERE session_id = :sid AND node_key = :anchor
                   AND status <> 'pending'
            """),
            {"sid": session_id, "anchor": before},
        )
    await db.execute(
        text("""
            UPDATE assist_sessions SET current_node_key = :nk, updated_at = NOW()
             WHERE id = :sid
        """),
        {"sid": session_id, "nk": new_key},
    )
    await db.commit()
    logger.info(
        "assist_added_step session_id=%s job_id=%s new=%s before=%s title=%r",
        session_id, job_id, new_key, before, drafted["title"],
    )
    return {
        "session_id": session_id, "node_key": new_key,
        "title": drafted["title"], "description": drafted["description"],
        "before_node_key": before,
    }


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
    from app.modules.assist_agent import _note_impact_facts_block, _note_impact_project_block  # §17.856 re-exports (patch-safe deferred)
    from datetime import datetime, timezone

    from app.config import settings
    from app.modules import assist_replan

    if not settings.assist_note_replan_enabled:
        return None
    # A generic 'note' is a reminder, not a plan-shaping requirement.
    if (note_kind or "note") == "note":
        return None
    sess = (await db.execute(
        text("SELECT job_id, status, metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess or sess["status"] not in ("active", "paused"):
        return None
    job_id = str(sess["job_id"])
    try:
        impact = await assist_replan.analyze_note_impact(
            db=db, job_id=job_id, note_text=note_text, note_kind=note_kind,
            facts_block=_note_impact_facts_block(sess.get("metadata")),  # §17.752
            project_recap_block=await _note_impact_project_block(job_id, db),  # §17.753
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
) -> dict | None:
    """§17.677/§17.693 — stash a pending_replan proposal on the session for the
    operator to confirm. Read-modify-write merge so other metadata keys survive;
    one pending proposal at a time (a fresh one overwrites any unresolved prior).

    §17.771 (Phase 4) — thrash-suppression: returns ``None`` WITHOUT staging when
    the operator already DISMISSED a proposal with this same signature. Pre-fix a
    discard cleared the pending key but left no memory, so the next matching turn
    re-surfaced the identical "Apply these plan changes?" — the "I said no, stop
    asking" loop the audit flagged. Covers both the message/pivot path
    (detect_reroute) and the explicit-note path (assess_note_impact), which both
    stage through here.
    """
    from datetime import datetime, timezone
    sig = _replan_signature(note_text, affected)
    meta_row = (await db.execute(
        text("SELECT metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if sig in _discarded_replans_from_metadata(meta_row.get("metadata") if meta_row else None):
        logger.info(
            "assist_replan_suppressed session_id=%s (operator already dismissed) sig=%r",
            session_id, sig[:60],
        )
        return None
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
    from app.modules.assist_agent import _note_impact_facts_block, _note_impact_project_block, sweep_superseded_facts  # §17.856 re-exports (patch-safe deferred)
    from app.config import settings
    from app.modules import assist_replan

    if (not settings.assist_pivot_detect_enabled
            or not settings.assist_note_replan_enabled
            or not (message or "").strip()):
        return None
    sess = (await db.execute(
        text("SELECT job_id, status, metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess or sess["status"] not in ("active", "paused"):
        return None
    job_id = str(sess["job_id"])
    try:
        impact = await assist_replan.analyze_note_impact(
            db=db, job_id=job_id, note_text=message, note_kind="decision",
            # §17.747 — a pivot can also invalidate ALREADY-DONE steps (e.g.
            # "delete the VM and recreate it" undoes the Ubuntu install / network
            # config on the old VM). Let the analyzer propose reopening them so
            # their stale "done" output stops leading the prompt as MANDATORY.
            include_done_reopen=settings.assist_pivot_reopen_enabled,
            # §17.763 — this is the FUZZY path (the classifier only weakly placed
            # this message as question/skip). Analyze conservatively so a plain
            # request for help isn't read as a plan-changing decision and surfaced
            # as a spurious re-plan. The explicit-note path stays liberal.
            strict=settings.assist_reroute_strict,
            facts_block=_note_impact_facts_block(sess.get("metadata")),  # §17.752
            project_recap_block=await _note_impact_project_block(job_id, db),  # §17.753
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
            node_key=None, dedupe=True, db=db,  # §17.854 C4 — no re-record on resend
        )
        # §17.854 (audit C2) — a reset/rebuild reached via a PIVOT message (not
        # the /note endpoint) must also sweep the abandoned-system facts, else
        # the append-only ledger keeps dragging dead state forward and
        # render_session_memory frames the whole ledger as "previous approach".
        # The sweep is valve-gated + fail-soft; harmless when not a reset.
        from app.modules import assist_guide as _ag
        if _ag._operator_reset_intent([{"kind": "decision", "text": message}]):
            await sweep_superseded_facts(
                session_id=session_id, note_text=message, db=db,
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


def _discarded_replans_from_metadata(metadata: Any) -> list[str]:
    """§17.771 (Phase 4) — signatures of re-plan proposals the operator already
    DISMISSED, so a matching one is not re-staged next turn (thrash-suppression)."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    dl = (metadata or {}).get("discarded_replans") if isinstance(metadata, dict) else None
    return [s for s in dl if isinstance(s, str)] if isinstance(dl, list) else []


def _replan_signature(note_text: str, affected: list) -> str:
    """§17.771 (Phase 4) — a stable fingerprint of a re-plan proposal: the
    normalized trigger text + the sorted set of affected node_keys. Two proposals
    with the same signature are "the same suggestion" for dedup purposes."""
    keys = sorted({
        str((a or {}).get("node_key", "")).strip()
        for a in (affected or []) if isinstance(a, dict)
    } - {""})
    norm = " ".join((note_text or "").lower().split())[:80]
    return f"{norm}|{','.join(keys)}"


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
        # §17.747 — a reopened node's prior output is nulled on dag_nodes, but the
        # operator's original submission is real history worth keeping. Preserve
        # it on the step's friction trail (also recoverable from assist_turns) so
        # the reopen is auditable and the redo starts informed.
        for nk, prior in (result.get("reopened_prior") or {}).items():
            if (prior or "").strip():
                try:
                    await record_friction(
                        session_id=session_id, node_key=nk,
                        note=("Reopened after operator pivot — prior result "
                              f"(preserved): {prior.strip()[:600]}"),
                        db=db,
                    )
                except Exception as e:  # noqa: BLE001 — preservation is best-effort
                    logger.warning("reopen_friction_preserve_failed nk=%s err=%r", nk, e)
        summary = {"applied": True, **result}
        # §17.866 — never leave the session pointing at a step the apply just
        # killed. The live incident: the operator's current step was in the
        # proposal's drop set; the apply skipped it but current_node_key kept
        # pointing there, so the UI re-rendered the dead step's stale
        # walkthrough forever ("refreshed and I'm on the same response").
        # Clearing the pointer makes the next /next claim fresh — which also
        # routes the claim through the §17.864 premise check.
        gone = set(result.get("dropped") or [])
        if gone:
            cur = (await db.execute(
                text("SELECT current_node_key FROM assist_sessions WHERE id = :sid"),
                {"sid": session_id},
            )).scalar()
            if cur and cur in gone:
                await db.execute(
                    text("UPDATE assist_sessions SET current_node_key = NULL "
                         "WHERE id = :sid"),
                    {"sid": session_id},
                )
                await db.commit()
                summary["current_step_cleared"] = cur
    else:
        summary = {"applied": False, "discarded": True}
        # §17.771 (Phase 4) — remember this dismissal so `_stage_replan_proposal`
        # doesn't re-surface the identical proposal next turn. Signature = trigger
        # text + affected node_keys; capped FIFO so the ledger can't grow unbounded.
        sig = _replan_signature(
            pending.get("note_text") or "", pending.get("proposals") or [],
        )
        discarded = _discarded_replans_from_metadata(sess.get("metadata"))
        if sig not in discarded:
            discarded = (discarded + [sig])[-20:]
            await db.execute(
                text("""
                    UPDATE assist_sessions
                       SET metadata = COALESCE(metadata, '{}'::jsonb)
                             || CAST(:patch AS jsonb)
                     WHERE id = :sid
                """),
                {"sid": session_id,
                 "patch": json.dumps({"discarded_replans": discarded})},
            )

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
