"""Assist Mode re-plan strategies.

Decides what to do when human-supplied output diverges from what the
autonomous executor would have produced. Three policies, configured
per-session on `assist_sessions.replan_policy`:

  context_only (DEFAULT) — no regeneration. Human evidence lands in
    `dag_nodes.output_text`; the existing upstream-last assembly forces
    downstream nodes to "build on" the actual upstream output. Handles
    most divergence implicitly; the verifier runs in the **background**
    so submit returns immediately. The `divergence` flag on
    `assist_steps` is observability data; tests should call
    `await drain_background_tasks()` to wait for it deterministically.

  selective                — regenerate only nodes that transitively depend
    on the changed node. Reuses the BFS in retry_failed_node.
    One LLM call, scoped to the affected subgraph. Synchronous because
    the result mutates state the user immediately depends on.

  full                     — regenerate all pending nodes. Discouraged;
    invalidates trust mid-session. Synchronous, same reason as selective.

  disabled                 — skip detection entirely.

The verifier is the same `model_verifier` (qwen2.5:7b) the autonomous
executor uses post-LLM, preserving the model-stack invariant.
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import text

from app.providers.base import Tool
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.assist.replan")

# W.10 — Track in-flight context_only divergence tasks so:
#   - tests can await completion deterministically via drain_background_tasks()
#   - tasks aren't garbage-collected mid-flight (asyncio.create_task only holds
#     a weak ref; a strong ref must live somewhere or the task can vanish).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


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

Call the record_divergence tool exactly once with your verdict."""


# Audit B4 — native tool-call schema for the divergence verifier. Replaces
# the W.6-era "Respond with a single JSON object…" coaxing prose. Mirrors
# the X.10 RECORD_VERIFICATION_TOOL pattern: schema lives in code, not in
# prompt prose; the wrapper parses structured args on native-tool providers
# and falls back to JSON-coaxing internally on non-native providers, so
# callers always read via `resp.tool_calls[0].arguments`.
RECORD_DIVERGENCE_TOOL = Tool(
    name="record_divergence",
    description=(
        "Report whether the human-supplied step output diverges from the "
        "task's intent."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "diverges": {
                "type": "boolean",
                "description": (
                    "True iff the human output substantively departs from "
                    "the task — different deliverable type, missing required "
                    "content, or a pivot to a different solution path. "
                    "Trivial wording / formatting differences are NOT divergence."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["minor", "major"],
                "description": (
                    "'major' when divergence is meaningful enough to trigger "
                    "downstream replan; 'minor' otherwise. Always emit when "
                    "diverges=true."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One short sentence explaining the verdict.",
            },
        },
        "required": ["diverges"],
    },
)


async def detect_divergence(
    *, title: str, prompt: str, evidence: str, model_overrides: dict | None = None,
) -> dict:
    """Run the divergence verifier. Returns the parsed dict.

    Audit B4 — migrated from `model_router.chat()` + `parse_json_object()`
    JSON-coaxing to `model_router.tool_call()` with the schema in
    ``RECORD_DIVERGENCE_TOOL``. Closes the last unmigrated JSON-coaxing
    site flagged by §17.9 / the audit's tool-call survey.

    Returns `{diverges: False, severity: 'minor', reason: 'detection_unavailable'|'detection_unparsed'}`
    on any failure (model unavailable, no tool_calls, missing 'diverges'
    key) — assist mode must not block on a flaky detector. Fail-closed
    contract is preserved across the migration.
    """
    # Defer the model_router import; it pulls heavy http client state.
    from app import model_router
    msg = _DIVERGENCE_PROMPT.format(
        title=title or "(untitled)",
        prompt=(prompt or "")[:4000],
        evidence=(evidence or "")[:4000],
    )
    try:
        # §17.89 Pattern 3 — dispatch via role= so MODEL_VERIFIER_PROVIDER is
        # honored. model_overrides flow through provider_for_role's overrides
        # arg so a per-request {model_verifier: <name>} still wins over the
        # default settings.model_verifier value.
        resp = await model_router.tool_call(
            messages=[{"role": "user", "content": msg}],
            tools=[RECORD_DIVERGENCE_TOOL],
            role="model_verifier",
            overrides=model_overrides,
            max_tokens=200,
        )
    except Exception as e:
        logger.warning("divergence_detector_failed: %s", e)
        return {"diverges": False, "severity": "minor", "reason": "detection_unavailable"}
    args = read_tool_args(resp)
    if not args or "diverges" not in args:
        logger.warning(
            "divergence_detector_unparsed: no diverges key in tool_calls "
            "(text_head=%r)",
            (getattr(resp, "text", "") or "")[:200],
        )
        return {"diverges": False, "severity": "minor", "reason": "detection_unparsed"}
    return {
        "diverges": bool(args.get("diverges")),
        "severity": args.get("severity") or "minor",
        "reason": str(args.get("reason", ""))[:200],
    }


# ── §17.677 — note-triggered impact analysis (surface-and-ask re-plan) ──────
#
# The submit path above compares a *finished* step's output against its own
# task. This path is different: the operator raises new information mid-session
# (a constraint/decision/addition/preference note) and we ask whether it
# invalidates any still-PENDING node's plan. We never rewrite; we return a
# proposal the operator confirms. Mirrors the detect_divergence pattern:
# native tool-call schema in code, model_verifier, fail-soft to no-op.

_NOTE_IMPACT_PROMPT = """The operator is executing a build plan step-by-step and \
has just told you a new {kind} that changes the situation:

  "{note}"

{brief}Here are the steps that have NOT been done yet. Use the project goals \
above to understand what each step actually involves (the titles are terse):
{nodes}

Your job: find every pending step whose current plan is now WRONG or UNNEEDED \
because of this {kind}. Reason concretely:
- If the {kind} forbids, removes, breaks, or changes a resource, device, tool, \
location, or approach that a step relies on, that step IS affected.
- action="drop" if the step is now unnecessary or impossible.
- action="revise" if the step must still happen but its approach/target/content \
has to change to respect the {kind}. Say concretely what changes.

Err toward flagging: a borderline step the operator can dismiss is far better \
than silently leaving a broken step in the plan. Only truly-unrelated steps are \
left out. If genuinely nothing is affected, return an empty list. Call \
record_plan_impact exactly once."""


RECORD_PLAN_IMPACT_TOOL = Tool(
    name="record_plan_impact",
    description=(
        "Report which still-pending plan steps a newly-raised operator "
        "constraint/decision/preference affects, and how each must change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "affected": {
                "type": "array",
                "description": (
                    "One entry per pending step the note materially changes. "
                    "Empty when the note leaves every pending step untouched."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "node_key": {
                            "type": "string",
                            "description": "The node_key of an affected pending step.",
                        },
                        "current_assumption": {
                            "type": "string",
                            "description": (
                                "The assumption in the current plan that the "
                                "note breaks, in one short phrase."
                            ),
                        },
                        "proposed_change": {
                            "type": "string",
                            "description": (
                                "For action='revise', the concrete change to "
                                "make to the step. For action='drop', why it is "
                                "no longer needed. One sentence."
                            ),
                        },
                        "action": {
                            "type": "string",
                            "enum": ["revise", "drop"],
                            "description": (
                                "'drop' if the step is now unnecessary/impossible; "
                                "'revise' if it should still happen but differently."
                            ),
                        },
                    },
                    "required": ["node_key", "action"],
                },
            },
        },
        "required": ["affected"],
    },
)


async def analyze_note_impact(
    *, db, job_id: str, note_text: str, note_kind: str,
    model_overrides: dict | None = None,
) -> dict:
    """§17.677 — ask whether a newly-raised note invalidates any pending node's
    plan. Returns ``{"affected": [{node_key, current_assumption, proposed_change,
    action}]}`` — only nodes the model flags, filtered to node_keys that are
    actually pending. Fail-soft: model error / no tool_call / no pending nodes →
    ``{"affected": []}`` so the note is still recorded and confirmed as before.
    """
    if not (note_text or "").strip():
        return {"affected": []}
    # Pending == still actionable. Exclude done/skipped/failed terminal work so
    # we never propose rewriting something the operator already finished.
    rows = (await db.execute(
        text("""
            SELECT node_key, title, description
              FROM dag_nodes
             WHERE job_id = :jid
               AND status = 'pending'
             ORDER BY node_key
        """),
        {"jid": job_id},
    )).mappings().all()
    if not rows:
        return {"affected": []}
    pending_keys = {r["node_key"] for r in rows}
    nodes_block = "\n".join(
        f"- {r['node_key']}: {r['title']}"
        + (f" — {(r['description'] or '')[:240]}" if r["description"] else "")
        for r in rows
    )
    # Brief goals/constraints ground the model in the project's intent.
    brief = ""
    job = (await db.execute(
        text("SELECT refined_brief FROM jobs WHERE id = :jid"),
        {"jid": job_id},
    )).mappings().first()
    rb = (job or {}).get("refined_brief") if job else None
    if isinstance(rb, str):
        try:
            rb = json.loads(rb)
        except (ValueError, TypeError):
            rb = None
    if isinstance(rb, dict):
        goals = rb.get("goals") or []
        cons = rb.get("constraints") or []
        lines = []
        if goals:
            lines.append("Project goals: " + "; ".join(str(g) for g in goals[:6]))
        if cons:
            lines.append("Existing constraints: " + "; ".join(str(c) for c in cons[:6]))
        if lines:
            brief = "\n".join(lines) + "\n\n"

    from app import model_router
    msg = _NOTE_IMPACT_PROMPT.format(
        kind=note_kind or "note",
        note=(note_text or "")[:2000],
        brief=brief,
        nodes=nodes_block[:6000],
    )
    try:
        resp = await model_router.tool_call(
            messages=[{"role": "user", "content": msg}],
            tools=[RECORD_PLAN_IMPACT_TOOL],
            # §17.677 — model_general (deepseek-v4-pro), NOT model_verifier. Live
            # smoke proved the verifier (kimi-code) reliably false-negatives here:
            # it emits a clean tool call with an empty `affected` list even when
            # the constraint plainly breaks steps. This is a reasoning task (map a
            # constraint onto a plan), which the general model does correctly. The
            # verifier's mock-based unit tests hid this — only a live call surfaced
            # it (cf. tool_call_needs_tool_objects).
            role="model_general",
            overrides=model_overrides,
            temperature=0.0,
            tool_choice="auto",
            # Generous budget: model_general reasons before the tool call, and a
            # tight cap returns empty content (see thinking_model_empty_content).
            max_tokens=4096,
        )
    except Exception as e:  # noqa: BLE001 — a flaky detector must never block
        logger.warning("note_impact_detector_failed: %s", e)
        return {"affected": []}
    args = read_tool_args(resp)
    raw = (args or {}).get("affected")
    if not isinstance(raw, list):
        logger.warning(
            "note_impact_unparsed: no affected list in tool_calls (text_head=%r)",
            (getattr(resp, "text", "") or "")[:200],
        )
        return {"affected": []}
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        nk = item.get("node_key")
        action = item.get("action")
        # Drop hallucinated node_keys and any node the model tags that isn't
        # actually pending — we only ever touch live, pending steps.
        if nk not in pending_keys or action not in ("revise", "drop"):
            continue
        out.append({
            "node_key": nk,
            "action": action,
            "current_assumption": str(item.get("current_assumption", ""))[:300],
            "proposed_change": str(item.get("proposed_change", ""))[:400],
        })
    return {"affected": out}


async def apply_note_replan(
    *, db, session_id: str, job_id: str, proposals: list[dict],
) -> dict:
    """§17.677 — apply a confirmed note-impact proposal to the *pending* plan.

    ``drop`` → mark the node (and its step) skipped. ``revise`` → append the
    proposed change to the node's description (a persistent plan fix, not just
    transient guidance text) and bust the step's cached walkthrough so the next
    /assist next regenerates it with the change in context. Every write is
    guarded on ``dag_nodes.status = 'pending'`` so already-finished work is never
    disturbed. Single transaction. Returns ``{"revised": [...], "dropped": [...]}``.
    """
    revise_keys = [p["node_key"] for p in proposals if p.get("action") == "revise"]
    drop_keys = [p["node_key"] for p in proposals if p.get("action") == "drop"]
    dropped: list[str] = []
    revised: list[str] = []

    if drop_keys:
        res = (await db.execute(
            text("""
                UPDATE dag_nodes
                   SET status = 'skipped', updated_at = NOW()
                 WHERE job_id = :jid
                   AND node_key = ANY(:keys)
                   AND status = 'pending'
             RETURNING node_key
            """),
            {"jid": job_id, "keys": drop_keys},
        )).mappings().all()
        dropped = [r["node_key"] for r in res]
        if dropped:
            await db.execute(
                text("""
                    UPDATE assist_steps
                       SET status = 'skipped', updated_at = NOW()
                     WHERE session_id = :sid
                       AND node_key = ANY(:keys)
                       AND status NOT IN ('committed', 'applied', 'handed_off')
                """),
                {"sid": session_id, "keys": dropped},
            )

    for p in proposals:
        if p.get("action") != "revise":
            continue
        nk = p["node_key"]
        change = (p.get("proposed_change") or p.get("current_assumption") or "").strip()
        if not change:
            change = "Revised to respect a newly-raised operator constraint."
        res = (await db.execute(
            text("""
                UPDATE dag_nodes
                   SET description = COALESCE(description, '')
                         || E'\n\n[Plan update — operator constraint]: ' || :change,
                       updated_at = NOW()
                 WHERE job_id = :jid
                   AND node_key = :nk
                   AND status = 'pending'
             RETURNING node_key
            """),
            {"jid": job_id, "nk": nk, "change": change},
        )).mappings().first()
        if res:
            revised.append(nk)
            # Bust any cached walkthrough so it regenerates with the change.
            await db.execute(
                text("""
                    UPDATE assist_steps
                       SET guidance = NULL,
                           guidance_meta = '{}'::jsonb,
                           guidance_status = 'none',
                           guidance_generated_at = NULL,
                           updated_at = NOW()
                     WHERE session_id = :sid AND node_key = :nk
                """),
                {"sid": session_id, "nk": nk},
            )

    await db.commit()
    logger.info(
        "assist_note_replan session_id=%s job_id=%s revised=%d dropped=%d",
        session_id, job_id, len(revised), len(dropped),
    )
    return {"revised": revised, "dropped": dropped}


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


async def all_pending_node_keys(
    *, db, job_id: str, exclude_node_key: str | None = None,
) -> list[str]:
    """§17.424 — every non-terminal node_key for a job (for policy='full').

    "Pending" here means not yet completed: ``status NOT IN ('done','skipped')``.
    Excludes ``exclude_node_key`` (the just-submitted root, which submit_step
    already flipped to 'done' — so it's naturally excluded too; the explicit
    skip is belt-and-suspenders). Empty list when nothing is left to do.
    """
    rows = (await db.execute(
        text("""
            SELECT node_key FROM dag_nodes
             WHERE job_id = :jid
               AND status NOT IN ('done', 'skipped')
        """),
        {"jid": job_id},
    )).mappings().all()
    return sorted(
        r["node_key"] for r in rows if r["node_key"] != exclude_node_key
    )


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
    affected_override: list[str] | None = None,
    scope: str = "selective",
) -> dict:
    """For policy='selective': identify the subgraph that depends on the
    changed node, regenerate prompt_template for those nodes via LLM
    (Sprint W.5), and reset their assist_steps + dag_nodes to pending so
    the user (or autonomous handoff) can re-walk them.

    §17.424 — ``affected_override`` lets policy='full' reuse this machinery
    with an explicit node set (all pending nodes) instead of the downstream
    BFS; ``scope`` labels the returned dict accordingly.

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
    if affected_override is not None:
        affected = affected_override
    else:
        affected = await downstream_node_keys(
            db=db, job_id=job_id, root_node_key=root_node_key,
        )
    if not affected:
        return {"affected_nodes": [], "scope": scope, "details": "no_dependents"}

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
                   -- §17.486 — the regenerated prompt_template describes a new
                   -- task; any cached walkthrough now describes the old one.
                   -- Clear it so the next /assist next regenerates fresh
                   -- guidance instead of serving a stale cache hit.
                   guidance = NULL,
                   guidance_meta = '{}'::jsonb,
                   guidance_status = 'none',
                   guidance_generated_at = NULL,
                   updated_at = NOW()
             WHERE session_id = :sid
               AND node_key = ANY(:keys)
               AND status <> 'skipped'
        """),
        {"sid": session_id, "keys": affected},
    )
    await db.commit()
    logger.info(
        "assist_replan scope=%s session_id=%s root=%s affected=%d severity=%s",
        scope, session_id, root_node_key, len(affected), divergence.get("severity"),
    )
    return {
        "affected_nodes": affected,
        "scope": scope,
        "severity": divergence.get("severity"),
        "reason": divergence.get("reason"),
        "regenerated_count": regen_result.get("regenerated", 0),
        "regen_errors": regen_result.get("errors", []),
    }


# ── Top-level dispatcher ───────────────────────────────────────────────────


async def _detect_and_mark_in_background(
    *,
    session_id: str,
    node_key: str,
    title: str,
    prompt: str,
    evidence: str,
    model_overrides: dict | None,
) -> None:
    """Background worker for context_only: run divergence detection and
    write the `divergence=TRUE` flag on assist_steps if applicable.

    Uses its own AsyncSession because the request-scoped session that
    spawned this task may have been closed by the time we run. Catches
    every exception so an Ollama outage / parse failure surfaces only
    in logs — never as an unhandled-task warning.
    """
    try:
        div = await detect_divergence(
            title=title, prompt=prompt, evidence=evidence,
            model_overrides=model_overrides,
        )
        if not div["diverges"] or div["severity"] != "major":
            return
        # Major divergence — mark the row. Open a fresh session because
        # the request session is gone.
        from app.database import async_session
        async with async_session() as bg_db:
            await bg_db.execute(
                text("""
                    UPDATE assist_steps SET divergence = TRUE, updated_at = NOW()
                     WHERE session_id = :sid AND node_key = :nk
                """),
                {"sid": session_id, "nk": node_key},
            )
            await bg_db.commit()
        logger.info(
            "assist_divergence_marked_async session_id=%s node=%s reason=%r",
            session_id, node_key, div.get("reason"),
        )
    except Exception as e:
        logger.warning(
            "assist_divergence_background_failed session_id=%s node=%s err=%r",
            session_id, node_key, e,
        )


async def drain_background_tasks() -> None:
    """Await all in-flight context_only divergence tasks.

    Tests should call this between submit and any assertion that reads
    `assist_steps.divergence`. In production, no caller waits — the
    task completes whenever the verifier returns and the flag lands
    asynchronously.
    """
    if not _BACKGROUND_TASKS:
        return
    pending = list(_BACKGROUND_TASKS)
    await asyncio.gather(*pending, return_exceptions=True)


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

    W.10: context_only's verifier call moved off the request path — it
    spawns a fire-and-forget asyncio task and returns None immediately.
    The `divergence` flag still lands on assist_steps, just ~3s later.
    selective/full stay synchronous because the result drives the BFS
    reset that the user immediately reads via /assist next.
    """
    if policy == "disabled":
        return None
    if policy == "context_only":
        # Fire-and-forget. Submit returns within milliseconds; the
        # verifier runs in the background. Strong ref via _BACKGROUND_TASKS
        # so the task isn't GC'd before completion.
        task = asyncio.create_task(
            _detect_and_mark_in_background(
                session_id=session_id, node_key=node_key,
                title=title, prompt=prompt, evidence=evidence,
                model_overrides=model_overrides,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        return None
    # selective / full: synchronous detection because the result drives
    # the BFS reset that the next /assist next call reads.
    div = await detect_divergence(
        title=title, prompt=prompt, evidence=evidence,
        model_overrides=model_overrides,
    )
    if not div["diverges"] or div["severity"] != "major":
        return None
    if policy == "selective":
        return await apply_selective_replan(
            db=db, session_id=session_id, job_id=job_id,
            root_node_key=node_key, root_evidence=evidence, divergence=div,
            model_overrides=model_overrides,
        )
    if policy == "full":
        # §17.424 — regenerate ALL pending nodes (not just the downstream
        # subgraph). Pre-§17.424 this passed the single root to the selective
        # machinery, so 'full' silently behaved identically to 'selective'
        # despite the policy name. Now it computes the full pending set.
        all_pending = await all_pending_node_keys(
            db=db, job_id=job_id, exclude_node_key=node_key,
        )
        return await apply_selective_replan(
            db=db, session_id=session_id, job_id=job_id,
            root_node_key=node_key, root_evidence=evidence, divergence=div,
            model_overrides=model_overrides,
            affected_override=all_pending, scope="full",
        )
    # The replan_policy column has a CHECK constraint (migration 023)
    # restricting values to context_only/selective/full/disabled. Reaching
    # here means the constraint was bypassed or the row pre-dates the
    # constraint — fail loud so the data is reconciled rather than silently
    # ignored.
    raise ValueError(f"unknown replan_policy: {policy!r}")
