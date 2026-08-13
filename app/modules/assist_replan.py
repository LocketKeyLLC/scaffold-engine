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


# §17.688 — a DECISION node's deliverable is a CHOICE, not a finished artifact.
# The default prompt treats "missing required content" (a decision node's task
# text names a full table/config the operator legitimately did not reproduce) as
# divergence, so a valid decision ("3 vlans") was flagged major → assist_steps
# .divergence=TRUE (and, under selective/full, a spurious downstream replan). A
# decision only diverges when it CONTRADICTS the framed choice or a hard
# constraint — not when it is merely terse or omits concrete implementation
# detail (that is applied by later steps).
_DIVERGENCE_PROMPT_DECISION = """You are checking whether a human operator's \
DECISION on a planning step diverges from what that step asked them to decide.

DECISION STEP TITLE: {title}
WHAT THE STEP ASKS THEM TO DECIDE (context — the concrete artifact it names is \
produced by LATER steps, NOT required in this answer): {prompt}

HUMAN DECISION (just submitted):
{evidence}

Decide: does the human's decision fit what this step asked them to choose?
- A clear, on-topic choice among the framed options (a count, an approach, a
  named option) is NOT divergence — even if it is terse and omits concrete
  values (IDs, subnets, commands, config); those are applied by later steps.
- Divergence is ONLY: contradicting a hard constraint the step set, choosing
  something outside the framed decision, or refusing/answering a different
  question entirely.

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
    is_decision: bool = False,
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
    from app.config import settings
    template = _DIVERGENCE_PROMPT_DECISION if is_decision else _DIVERGENCE_PROMPT
    msg = template.format(
        title=title or "(untitled)",
        prompt=(prompt or "")[:4000],
        evidence=(evidence or "")[:4000],
    )
    try:
        # §17.89 Pattern 3 — dispatch via role= so the role's PROVIDER is honored.
        # model_overrides flow through provider_for_role's overrides arg so a
        # per-request {<role>: <name>} still wins over the default settings value.
        # §17.771 (Phase 0) — role is now settings-driven (default model_general,
        # was hardcoded model_verifier/kimi). max_tokens bumped 200→400: the
        # verdict itself is tiny, but deepseek's tool-call framing needs a little
        # more headroom than kimi's to avoid a truncated call → unparsed → silent
        # under-react.
        resp = await model_router.tool_call(
            messages=[{"role": "user", "content": msg}],
            tools=[RECORD_DIVERGENCE_TOOL],
            role=settings.assist_divergence_model_role,
            overrides=model_overrides,
            max_tokens=400,
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

# §17.763 — the opening framing and the closing bias paragraph are parametrized
# (``strict``) so the two callers can run this analyzer with OPPOSITE priors:
#   • assess_note_impact (§17.677) — the operator EXPLICITLY recorded a
#     constraint/decision/preference note, so err TOWARD flagging (liberal).
#   • detect_reroute (§17.693) — a FUZZY reroute over a message the turn
#     classifier merely couldn't confidently place (question/skip). Fed the
#     liberal prompt, it hallucinated plan impact from a plain request for help
#     and surfaced a spurious re-plan ("🔀 …Apply these plan changes?") — the
#     reported "asked for help, it reverted to DAG planning" bug. That path now
#     runs CONSERVATIVELY: flag only a concrete situation-fact that contradicts a
#     specific step; a help/how-to/confusion message is NOT a plan change.
_NOTE_IMPACT_OPENING = ("The operator is executing a build plan step-by-step and "
                        "has just told you a new {kind} that changes the situation:")

_REROUTE_OPENING = (
    "The operator is executing a build plan step-by-step. They just sent the "
    "message below. It MIGHT reshape the plan — but it might equally be a request "
    "for help, a how-to question, confusion, or a comment about the CURRENT step. "
    "Read it, decide which, and report ONLY genuine plan impact:")

_NOTE_IMPACT_BIAS = (
    "Err toward flagging: a borderline step the operator can dismiss is far better "
    "than silently leaving a broken step in the plan. Only truly-unrelated steps "
    "are left out. If genuinely nothing is affected, return an empty list. Call "
    "record_plan_impact exactly once.")

_REROUTE_BIAS = (
    "Err toward LEAVING THE PLAN ALONE. Flag a step ONLY when the message states a "
    "CONCRETE fact or constraint about the operator's REAL situation that directly "
    "contradicts that step's assumption — e.g. 'I already have Proxmox installed' "
    "against a step that reinstalls it, or 'this host has no second NIC' against a "
    "step that assumes one. A request for HELP or a HOW-TO ('help me get the "
    "bridge working', 'how do I configure X', 'I'm stuck on Y', 'can you walk me "
    "through this'), an expression of confusion, a clarifying question, or anything "
    "about how to DO the current step is NOT a plan change — return an empty list "
    "for those. If the message does not clearly invalidate a SPECIFIC pending "
    "step, return an empty list. Call record_plan_impact exactly once.")

_NOTE_IMPACT_PROMPT = """{opening}

  "{note}"

{project}{facts}{brief}Here are the steps that have NOT been done yet. Use the project goals \
above to understand what each step actually involves (the titles are terse):
{nodes}

Your job: find every pending step whose current plan is now WRONG or UNNEEDED \
because of this {kind}. Reason concretely:
- If the {kind} forbids, removes, breaks, or changes a resource, device, tool, \
location, or approach that a step relies on, that step IS affected.
- action="drop" if the step is now unnecessary or impossible.
- action="revise" if the step must still happen but its approach/target/content \
has to change to respect the {kind}. Say concretely what changes.

{bias}"""


# §17.747 — pivot-triggered done-node reopening. Appended to the note-impact
# prompt ONLY on a pivot (detect_reroute), so a routine note never risks
# throwing away finished work. The asymmetry is deliberate: for PENDING nodes we
# "err toward flagging" (a wrong revise is cheap to dismiss); for DONE nodes we
# "err toward LEAVING DONE" (a wrong reopen sends the operator back to redo
# real work). The classic trigger is "delete VM 100 and recreate it" — that
# destroys everything installed/configured on the old VM, so those completed
# steps are no longer true and must be reopened.
_DONE_REOPEN_SUFFIX = """

Separately: the operator's {kind} may also UNDO work that is ALREADY MARKED DONE \
— e.g. deleting and recreating a machine destroys everything that was installed \
or configured on the OLD machine, so those completed steps are no longer true. \
Here are the steps already marked DONE, each with a short summary of what it \
produced:
{done_nodes}

For each done step whose completed RESULT the {kind} destroys or invalidates (so \
it would have to be redone from scratch), add an entry with action="reopen". Be \
CONSERVATIVE — reopening throws finished work away and sends the operator back to \
redo it. Only reopen a step when its result clearly no longer holds (the thing it \
built or configured is gone or is being replaced). A done step about a DIFFERENT \
resource the change does not touch (e.g. host-level setup when only a guest VM is \
being rebuilt) stays DONE — do NOT reopen it. When unsure, leave it done."""


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
                                "no longer needed. For action='reopen', what the "
                                "note destroyed so the completed step must be "
                                "redone. One sentence."
                            ),
                        },
                        "action": {
                            "type": "string",
                            "enum": ["revise", "drop", "reopen"],
                            "description": (
                                "'drop' if a pending step is now "
                                "unnecessary/impossible; 'revise' if a pending "
                                "step should still happen but differently; "
                                "'reopen' if an ALREADY-DONE step's result was "
                                "destroyed/invalidated by the note and must be "
                                "redone from scratch."
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
    include_done_reopen: bool = False,
    strict: bool = False,
    facts_block: str = "",
    project_recap_block: str = "",
) -> dict:
    """§17.677 — ask whether a newly-raised note invalidates any pending node's
    plan. Returns ``{"affected": [{node_key, current_assumption, proposed_change,
    action}]}`` — only nodes the model flags, filtered to node_keys that are
    actually pending. Fail-soft: model error / no tool_call / no pending nodes →
    ``{"affected": []}`` so the note is still recorded and confirmed as before.

    §17.747 — with ``include_done_reopen`` (set only on a PIVOT, via
    ``detect_reroute``), ALSO examine already-DONE nodes and let the model
    propose ``action='reopen'`` for any whose completed result the pivot
    destroys (e.g. "delete VM 100 and recreate" undoes everything installed on
    the old VM). Reopening a done node resets it to pending so its now-false
    output stops being injected as MANDATORY upstream context. Conservative by
    construction (see ``_DONE_REOPEN_SUFFIX``) and still surface-and-ask — the
    operator confirms before any finished work is reset.
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
    # §17.747 — done nodes are candidates for reopening on a pivot only.
    done_rows = []
    if include_done_reopen:
        done_rows = (await db.execute(
            text("""
                SELECT node_key, title, output_text
                  FROM dag_nodes
                 WHERE job_id = :jid
                   AND status = 'done'
                 ORDER BY node_key
            """),
            {"jid": job_id},
        )).mappings().all()
    if not rows and not done_rows:
        return {"affected": []}
    pending_keys = {r["node_key"] for r in rows}
    done_keys = {r["node_key"] for r in done_rows}
    nodes_block = "\n".join(
        f"- {r['node_key']}: {r['title']}"
        + (f" — {(r['description'] or '')[:240]}" if r["description"] else "")
        for r in rows
    ) or "(none — every step is already done)"
    done_block = "\n".join(
        f"- {r['node_key']}: {r['title']}"
        + (f" — produced: {(r['output_text'] or '')[:240]}" if r["output_text"] else "")
        for r in done_rows
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

    # §17.752 — whether the note actually breaks a step often depends on the
    # operator's REAL system (e.g. "no TPM" only invalidates a step that assumed
    # one). Ground the analyzer in the observed facts ledger, not just the brief.
    facts = ""
    if (facts_block or "").strip():
        facts = ("The operator's ACTUAL system (observed — judge impact against "
                 "this reality, not a generic setup):\n" + facts_block.strip() + "\n\n")
    # §17.753 — the distilled whole-project state so the analyzer judges impact
    # against the ARC (what earlier steps decided / already built), not just the
    # pending list — e.g. a pivot away from something already established.
    project = ""
    if (project_recap_block or "").strip():
        project = project_recap_block.strip() + "\n\n"
    from app import model_router
    # §17.763 — strict (fuzzy-reroute) vs liberal (explicit-note) priors.
    opening = (_REROUTE_OPENING if strict else _NOTE_IMPACT_OPENING).format(
        kind=note_kind or "note",
    )
    bias = _REROUTE_BIAS if strict else _NOTE_IMPACT_BIAS
    msg = _NOTE_IMPACT_PROMPT.format(
        kind=note_kind or "note",
        opening=opening,
        bias=bias,
        note=(note_text or "")[:2000],
        project=project,
        facts=facts,
        brief=brief,
        nodes=nodes_block[:6000],
    )
    # §17.747 — on a pivot, append the done-node reopening section so the model
    # can flag completed steps the pivot destroyed.
    if done_rows:
        msg += _DONE_REOPEN_SUFFIX.format(
            kind=note_kind or "note", done_nodes=done_block[:6000],
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
        # Drop hallucinated node_keys and mismatched actions. revise/drop only
        # touch PENDING nodes; reopen (§17.747) only touches DONE nodes — a model
        # that tags the wrong status is ignored so we never reset the wrong node.
        if action in ("revise", "drop"):
            if nk not in pending_keys:
                continue
        elif action == "reopen":
            if nk not in done_keys:
                continue
        else:
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
    /assist next regenerates it with the change in context. §17.747 ``reopen`` →
    reset an already-DONE node (its result destroyed by a pivot) back to pending,
    returning its prior output so the caller can preserve it. drop/revise are
    guarded on ``status='pending'`` and reopen on ``status='done'`` so the wrong
    node is never touched. Single transaction. Returns ``{"revised", "dropped",
    "reopened", "reopened_prior"}``.
    """
    revise_keys = [p["node_key"] for p in proposals if p.get("action") == "revise"]
    drop_keys = [p["node_key"] for p in proposals if p.get("action") == "drop"]
    reopen_keys = [p["node_key"] for p in proposals if p.get("action") == "reopen"]
    dropped: list[str] = []
    revised: list[str] = []
    reopened: list[str] = []
    reopened_prior: dict[str, str] = {}

    # §17.747 — reopen done nodes the pivot invalidated: capture the prior output
    # (so the caller can preserve it), then reset the node + its step to pending.
    # Guarded on status='done' so a concurrent change can never reset the wrong
    # thing. Resetting to pending drops the node from fetch_upstream_outputs
    # (done-only), so its now-false result stops being injected as MANDATORY
    # upstream context — the structural fix the §17.746 recap-authority header
    # only mitigated at the prompt level.
    if reopen_keys:
        prior = (await db.execute(
            text("""
                SELECT node_key, output_text FROM dag_nodes
                 WHERE job_id = :jid AND node_key = ANY(:keys) AND status = 'done'
            """),
            {"jid": job_id, "keys": reopen_keys},
        )).mappings().all()
        reopened_prior = {r["node_key"]: (r["output_text"] or "") for r in prior}
        res = (await db.execute(
            text("""
                UPDATE dag_nodes
                   SET status = 'pending',
                       output_text = NULL,
                       completed_at = NULL,
                       updated_at = NOW()
                 WHERE job_id = :jid
                   AND node_key = ANY(:keys)
                   AND status = 'done'
             RETURNING node_key
            """),
            {"jid": job_id, "keys": reopen_keys},
        )).mappings().all()
        reopened = [r["node_key"] for r in res]
        if reopened:
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
                           guidance = NULL,
                           guidance_meta = '{}'::jsonb,
                           guidance_status = 'none',
                           guidance_generated_at = NULL,
                           updated_at = NOW()
                     WHERE session_id = :sid
                       AND node_key = ANY(:keys)
                       AND status <> 'skipped'
                """),
                {"sid": session_id, "keys": reopened},
            )

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
        "assist_note_replan session_id=%s job_id=%s revised=%d dropped=%d reopened=%d",
        session_id, job_id, len(revised), len(dropped), len(reopened),
    )
    return {
        "revised": revised, "dropped": dropped,
        "reopened": reopened, "reopened_prior": reopened_prior,
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


async def stage_divergence_replan(
    *, db, session_id: str, job_id: str, node_key: str, title: str,
    evidence: str, reason: str, model_overrides: dict | None = None,
) -> dict | None:
    """§17.699 — turn a detected MAJOR divergence into a surface-and-ask re-plan.

    The submit-path divergence verifier already knows a just-committed step's
    evidence departs from its plan; on context_only that only set an invisible
    ``divergence=TRUE`` flag. Here we treat the operator's ACTUAL result as new
    information about the system's real state and run the §17.677 note-impact
    analyzer over the still-pending nodes. When ≥1 pending node is invalidated,
    stage a ``pending_replan`` proposal (``note_kind='divergence'``,
    ``surfaced=False``) so /assist next surfaces it once and the operator
    resolves it with the same yes/no confirm+apply path as a note or pivot.

    Fail-soft and self-gating: disabled by valve, non-active session, no
    affected pending nodes, or any error → returns None and stages nothing.
    Never rewrites the plan directly; only proposes.
    """
    from datetime import datetime, timezone

    from app.config import settings

    if not settings.assist_divergence_replan_enabled:
        return None
    sess = (await db.execute(
        text("SELECT status, metadata FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    if not sess or sess["status"] not in ("active", "paused"):
        return None
    # §17.752 — ground the analyzer in the operator's observed system facts.
    facts_block = ""
    if settings.assist_note_impact_facts_aware:
        from app.modules import assist_guide
        md = sess.get("metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except (ValueError, TypeError):
                md = {}
        env = (md or {}).get("environment") if isinstance(md, dict) else {}
        facts_block = assist_guide.render_facts_block(env)
    # Frame the operator's real result (and why it diverged) as a decision note
    # about the true system state — the analyzer maps that onto the pending plan.
    note_text = (
        f"While working the step \"{title or node_key}\", my actual result was: "
        f"{(evidence or '').strip()[:1500]}"
    )
    if reason:
        note_text += f"\n\n(This differs from that step's original plan: {reason})"
    try:
        # §17.753 — ground the analyzer in the whole-project arc too.
        from app.modules import assist_agent
        project_recap_block = assist_guide.render_project_recap_block(
            await assist_agent.get_project_recap(job_id=job_id, db=db))
        impact = await analyze_note_impact(
            db=db, job_id=job_id, note_text=note_text, note_kind="decision",
            model_overrides=model_overrides, facts_block=facts_block,  # §17.752
            project_recap_block=project_recap_block,  # §17.753
        )
    except Exception as e:  # noqa: BLE001 — a flaky analyzer must never surface
        logger.warning("divergence_replan_analyze_failed session_id=%s err=%r", session_id, e)
        return None
    affected = impact.get("affected") or []
    if not affected:
        return None
    # §17.771 (deferred, now done) — divergence-path thrash-suppression: if the
    # operator already DISMISSED an equivalent proposal, don't re-stage it (parity
    # with _stage_replan_proposal on the message path — this path writes
    # pending_replan directly, so it needs the same guard). Same signature; the
    # discard ledger is shared (apply_pending_replan records ANY discarded proposal).
    from app.modules import assist_agent
    sig = assist_agent._replan_signature(note_text, affected)
    if sig in assist_agent._discarded_replans_from_metadata(sess.get("metadata")):
        logger.info(
            "divergence_replan_suppressed session_id=%s (operator already dismissed)",
            session_id,
        )
        return None
    proposal = {
        "note_text": note_text,
        "note_kind": "divergence",
        "source_node": node_key,
        "reason": reason,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proposals": affected,
        "surfaced": False,
    }
    # Read-modify-write merge so sibling metadata keys survive; one pending
    # proposal at a time (a fresh one overwrites any unresolved prior — matches
    # _stage_replan_proposal's latest-wins contract).
    await db.execute(
        text("""
            UPDATE assist_sessions
               SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb),
                   updated_at = NOW()
             WHERE id = :sid
        """),
        {"sid": session_id, "patch": json.dumps({"pending_replan": proposal})},
    )
    await db.commit()
    logger.info(
        "assist_divergence_replan_proposed session_id=%s node=%s affected=%d",
        session_id, node_key, len(affected),
    )
    return proposal


async def _detect_and_mark_in_background(
    *,
    session_id: str,
    job_id: str,
    node_key: str,
    title: str,
    prompt: str,
    evidence: str,
    model_overrides: dict | None,
    is_decision: bool = False,
) -> None:
    """Background worker for context_only: run divergence detection and
    write the `divergence=TRUE` flag on assist_steps if applicable.

    §17.699 — on a MAJOR divergence, also stage a surface-and-ask re-plan
    proposal (``stage_divergence_replan``) so the operator is proactively asked
    whether to fix the now-inconsistent pending plan, instead of the divergence
    being a flag they never see.

    Uses its own AsyncSession because the request-scoped session that
    spawned this task may have been closed by the time we run. Catches
    every exception so an Ollama outage / parse failure surfaces only
    in logs — never as an unhandled-task warning.
    """
    try:
        div = await detect_divergence(
            title=title, prompt=prompt, evidence=evidence,
            model_overrides=model_overrides, is_decision=is_decision,
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
            # §17.699 — proactively propose a plan fix from the divergence.
            await stage_divergence_replan(
                db=bg_db, session_id=session_id, job_id=job_id,
                node_key=node_key, title=title, evidence=evidence,
                reason=div.get("reason") or "", model_overrides=model_overrides,
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
    is_decision: bool = False,
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
                session_id=session_id, job_id=job_id, node_key=node_key,
                title=title, prompt=prompt, evidence=evidence,
                model_overrides=model_overrides, is_decision=is_decision,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        return None
    # selective / full: synchronous detection because the result drives
    # the BFS reset that the next /assist next call reads.
    div = await detect_divergence(
        title=title, prompt=prompt, evidence=evidence,
        model_overrides=model_overrides, is_decision=is_decision,
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
