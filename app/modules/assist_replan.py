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
        # Treat as "select all pending" — implemented via the same
        # selective machinery with the entire pending set.
        return await apply_selective_replan(
            db=db, session_id=session_id, job_id=job_id,
            root_node_key=node_key, root_evidence=evidence, divergence=div,
            model_overrides=model_overrides,
        )
    # The replan_policy column has a CHECK constraint (migration 023)
    # restricting values to context_only/selective/full/disabled. Reaching
    # here means the constraint was bypassed or the row pre-dates the
    # constraint — fail loud so the data is reconciled rather than silently
    # ignored.
    raise ValueError(f"unknown replan_policy: {policy!r}")
