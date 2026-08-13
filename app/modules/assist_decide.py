"""§17.771 (Phase 1) — the unified assist decision.

ONE context-rich decision per operator turn, replacing the fragmented
decision-making the §17.771 audit found: ~8 phrase-regex gates in the pipeline
(`_assist_handlers.assist_nl_turn`) + `classify_turn` + `_track_progress` +
`reroute`/`analyze_note_impact`, none of which sees what the others see, wired
in an order the code itself flags "load-bearing" three times.

`decide_turn` assembles the WHOLE situation once — the current step, the recent
dialogue, the distilled facts / environment / operator notes / whole-project
digest / running recap (via the §17.751 `assemble_generation_memory` funnel),
plus the high-precision deterministic signals the pipeline gates encode (a
pasted shell prompt, a shell error, whether the last assistant turn was a fix)
passed in as FEATURES rather than re-inferred — and emits a single structured
``Decision``:

    action        one primary route (the ASSIST_INTENTS vocabulary + add_step)
    evidence/…    the params that route carries (evidence, error_text, query, note_*)
    plan_impact   orthogonal: does this ALSO reshape the plan? none|surface|reshape
    suggestion    on a decision step: a committed leaning + why (still the operator's call)
    confidence    low|medium|high — LOW is the Phase-2 signal to fall back to the
                  deterministic cascade (the operator chose to keep it as a safety net)
    rationale     one line, for the friction-log shadow record / debugging

Phase 1 ships this behind ``settings.assist_unified_decision_enabled`` (default
OFF) in SHADOW mode: the live pipeline is unchanged; `classify_session_turn`
fires `decide_turn` as a fire-and-forget background task and logs the
Decision-vs-classifier comparison to the friction log, so we gather real-turn
parity data with zero user-facing risk. Phase 2 flips the pipeline to dispatch
on the Decision, keeping the cascade as the low-confidence/error fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from sqlalchemy import text

from app import model_router
from app.config import settings
from app.modules import assist_guide
from app.modules.assist_guide import ASSIST_INTENTS

logger = logging.getLogger("scaffold")

# The primary-route vocabulary: the existing classifier intents plus `add_step`
# (the §17.736 "make this a proper step" route the tracker used to reach via a
# separate /track call). One action = one dispatch target in Phase 2.
DECIDE_ACTIONS = tuple(ASSIST_INTENTS) + ("add_step",)
_PLAN_IMPACTS = ("none", "surface", "reshape")
_CONFIDENCE = ("low", "medium", "high")
_NOTE_KINDS = ("addition", "constraint", "preference", "decision", "note")

# ── Deterministic signals (features fed to the model, NOT decisions) ──────────
# Mirror the pipeline's high-precision gates so the one decision reasons WITH
# them instead of the pipeline pre-empting the model. Same shapes as
# `_assist_handlers._SHELL_PROMPT_LINE_RE` / `_SHELL_ERROR_RE` and the §17.748
# fix marker, kept here so the server can compute them from (message, history).
_SHELL_PROMPT_LINE_RE = re.compile(r"(?m)^\s*[A-Za-z_][\w.-]*@[\w.-]+:[^\n#$]*[#$]")
_SHELL_ERROR_RE = re.compile(
    r"command not found|no such file or directory|permission denied"
    r"|operation not permitted|traceback \(most recent call last\)"
    r"|cannot (?:open|access|stat|create|remove|find|execute|connect|locate)"
    r"|could not (?:open|find|resolve|connect|create|load)|unable to |failed to "
    r"|\bnot recognized\b|unknown (?:option|command|argument|flag)"
    r"|invalid (?:option|argument|parameter|value|name)"
    r"|connection (?:refused|timed out|reset)|does(?:n't| not) exist|no space left",
    re.I | re.M,
)


def _compute_signals(message: str, history: list[dict] | None) -> dict:
    """The deterministic features the decision reasons with (§17.705/748/749)."""
    msg = message or ""
    is_shell_paste = bool(_SHELL_PROMPT_LINE_RE.search(msg))
    last_was_fix = False
    for m in reversed(history or []):
        if not isinstance(m, dict) or (m.get("role") or "") != "assistant":
            continue
        c = m.get("content") or ""
        last_was_fix = ("🔧 Troubleshooting" in c) or (
            "something went wrong — let me help" in c
        )
        break
    return {
        "shell_paste": is_shell_paste,
        "shell_error": bool(_SHELL_ERROR_RE.search(msg)),
        "last_assistant_was_fix": last_was_fix,
    }


# ── The decision prompt (built FROM the classifier prompt to avoid drift) ─────
# The routing distinctions (submit vs fix vs ask vs question vs note vs handoff)
# live in ONE place — `assist_guide._CLASSIFY_SYSTEM`. We reuse them verbatim and
# append only the NEW reasoning the unified decision adds: progress→action,
# plan_impact, the decision-step suggestion, and calibrated confidence.
_DECIDE_EXTRA = (
    "\n\nYou make the WHOLE decision for this turn in one shot — not just the "
    "route, but three more things:\n\n"
    "PROGRESS → the right action. Reconcile where the operator ACTUALLY is with "
    "the step pointer:\n"
    "- Still working this step (a question, a partial result, a refinement) → the "
    "route above (question/ask/submit/…).\n"
    "- They've clearly FINISHED this step ('done', 'that worked', a clean success "
    "paste with no error) → advance.\n"
    "- They've moved on to a foundational sub-task the plan has NO step for (e.g. "
    "the box has no network yet and nothing covers it), or they ask to make one → "
    "add_step.\n"
    "- Everything in the plan is done → finalize.\n\n"
    "PLAN_IMPACT (orthogonal — set it EVEN when the action is ask/question):\n"
    "- 'none' — the message is about DOING the current work: a how-to, a request "
    "for help, a clarification, a result. A REQUEST FOR HELP OR A HOW-TO IS NOT A "
    "PLAN CHANGE ('help me get the bridge up', 'how do I configure X', \"I'm stuck "
    "on Y\") → none. Err strongly toward none.\n"
    "- 'surface' — the operator states a CONCRETE fact/constraint about their REAL "
    "situation that likely invalidates a PENDING step ('I only have 2 NICs', 'this "
    "box has no IPMI') — worth surfacing for them to confirm.\n"
    "- 'reshape' — an explicit directional change ('use ZFS instead', 'scrap the "
    "VLANs, one flat network') that plainly rewrites pending steps.\n\n"
    "SUGGESTION — only when the current step IS A DECISION (a choice the operator "
    "must make) AND they haven't already chosen. Give a committed leaning and the "
    "ONE main reason, tailored to their stated environment/facts. Decisive but "
    "never final: it is their call. Omit for non-decision steps or once they've "
    "picked.\n\n"
    "CONFIDENCE — how sure you are of `action`. Use 'low' when the message is "
    "genuinely ambiguous or you're guessing; 'low' tells the caller to fall back "
    "to its deterministic routing. Be honest — a false 'high' on a wrong action is "
    "worse than an honest 'low'.\n\n"
    "RATIONALE — one short sentence explaining the call (for debugging).\n\n"
    "The SIGNALS line gives you high-precision facts already computed from the "
    "message: shell_paste (the message contains a real shell prompt line — it is "
    "the operator reporting a result), shell_error (that paste contains an error — "
    "the step is NOT done, route to fix), last_assistant_was_fix (your previous "
    "turn was a troubleshooting fix, so a paste now is a diagnostic REPLY, route "
    "to fix — do not advance past a broken command). Trust these.\n\n"
    "Call record_decision exactly once."
)


def _decide_system(is_decision: bool) -> str:
    base = assist_guide._CLASSIFY_SYSTEM.replace(
        "Call classify_turn exactly once.", ""
    ).rstrip()
    hint = assist_guide._CLASSIFY_DECISION_HINT if is_decision else ""
    return base + hint + _DECIDE_EXTRA


_RECORD_DECISION_TOOL = model_router.Tool(
    name="record_decision",
    description=(
        "Record the single decision for this assist turn: the primary action, "
        "its parameters, whether it also changes the plan, an optional decision "
        "suggestion, your confidence, and a one-line rationale."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(DECIDE_ACTIONS),
                "description": "The one primary route for this turn.",
            },
            "evidence": {
                "type": "string",
                "description": "For submit: what the operator did/decided (their result).",
            },
            "error_text": {
                "type": "string",
                "description": "For fix: the failure / error the operator hit.",
            },
            "query": {
                "type": "string",
                "description": "For ask: the researchable question to answer.",
            },
            "note_text": {
                "type": "string",
                "description": "For note: the requirement/constraint to carry forward.",
            },
            "note_kind": {
                "type": "string",
                "enum": list(_NOTE_KINDS),
                "description": "For note: the kind of note.",
            },
            "plan_impact": {
                "type": "string",
                "enum": list(_PLAN_IMPACTS),
                "description": "Does this ALSO reshape the plan? Default none.",
            },
            "suggestion": {
                "type": "object",
                "description": "Only for an unresolved decision step.",
                "properties": {
                    "leaning": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
            "confidence": {
                "type": "string",
                "enum": list(_CONFIDENCE),
                "description": "How sure you are of `action`.",
            },
            "rationale": {
                "type": "string",
                "description": "One short sentence explaining the decision.",
            },
        },
        "required": ["action", "confidence", "rationale"],
    },
)


def _fallback_decision(reason: str) -> dict:
    """Fail-soft Decision — low confidence so the caller uses its own routing."""
    return {
        "action": "question", "evidence": "", "error_text": "", "query": "",
        "note_text": "", "note_kind": "note", "plan_impact": "none",
        "suggestion": None, "confidence": "low", "rationale": reason,
        "node_key": None, "title": None, "is_decision": False,
        "signals": {}, "unavailable": True,
    }


async def decide_turn(
    *, session_id: str, message: str, node_key: str | None = None,
    history: list[dict] | None = None, db,
) -> dict:
    """The unified assist decision. Returns a validated Decision dict.

    Fail-soft everywhere: an unresolvable session/step or any model/parse error
    returns a low-confidence `question` decision (``unavailable=True``) so a
    flaky decision never blocks the turn — the caller falls back to its
    deterministic routing exactly as it does today.
    """
    from app.modules import assist_agent

    if not (message or "").strip():
        return _fallback_decision("empty_message")

    sess = (await db.execute(
        text("""
            SELECT id, job_id, status, current_node_key, metadata, notes
              FROM assist_sessions WHERE id = :sid
        """),
        {"sid": session_id},
    )).mappings().first()
    if not sess or sess["status"] not in ("active", "paused"):
        return _fallback_decision("session_not_steppable")

    nk = node_key or sess["current_node_key"]
    if not nk:
        return _fallback_decision("no_current_step")

    try:
        node_row, ctx = await assist_agent._assemble_ctx_for_node(
            db=db, job_id=str(sess["job_id"]), node_key=nk,
        )
    except ValueError:
        return _fallback_decision("step_unresolvable")

    kind = assist_agent._collect_step_kind(node_row.get("node_type"), ctx.base_prompt)
    is_decision = kind == "decision"
    signals = _compute_signals(message, history)

    # The full "sees everything" bundle — the §17.751 one funnel.
    try:
        mem = await assist_agent.assemble_generation_memory(
            session_id=session_id, nk=nk, sess=dict(sess), db=db, ctx=ctx,
            history=history, title=ctx.title,
        )
        env_block = assist_guide.render_environment_block(mem.environment)
        notes_block = assist_guide.render_operator_notes_block(mem.operator_notes)
        conversation = mem.conversation or ""
        digest = mem.job_digest or ""
    except Exception as exc:  # never let context assembly sink the decision
        logger.warning("decide_turn_context_failed session=%s err=%r", session_id, exc)
        env_block = notes_block = conversation = digest = ""

    parts = [f"Current step: {ctx.title}"]
    if kind:
        parts.append(f"(This step is a {kind} step.)")
    parts.append(f"\nWhat the step asks:\n{(ctx.base_prompt or '')[:1500]}")
    parts.append(f"\nTool for this step: {ctx.tool or 'LLM'}")
    if env_block.strip():
        parts.append(f"\n{env_block.strip()}")
    if notes_block.strip():
        parts.append(f"\n{notes_block.strip()}")
    if digest.strip():
        parts.append(f"\nProject so far:\n{digest.strip()[:2000]}")
    if conversation.strip():
        parts.append(f"\nRecent conversation:\n{conversation.strip()[:2000]}")
    parts.append(
        "\nSignals: "
        f"shell_paste={signals['shell_paste']}, "
        f"shell_error={signals['shell_error']}, "
        f"last_assistant_was_fix={signals['last_assistant_was_fix']}"
    )
    parts.append(f"\nOperator's message:\n{(message or '')[:2000]}")
    user = "\n".join(parts)

    try:
        resp = await model_router.tool_call(
            [
                {"role": "system", "content": _decide_system(is_decision)},
                {"role": "user", "content": user},
            ],
            [_RECORD_DECISION_TOOL],
            role=settings.assist_decide_model_role,
            temperature=0.0,
            max_tokens=768,
            tool_choice="auto",
        )
    except Exception as exc:
        logger.warning("decide_turn_model_failed session=%s err=%r", session_id, exc)
        return {**_fallback_decision("model_error"), "node_key": nk,
                "title": ctx.title, "is_decision": is_decision, "signals": signals}

    if not resp.success or not resp.tool_calls:
        return {**_fallback_decision("no_tool_call"), "node_key": nk,
                "title": ctx.title, "is_decision": is_decision, "signals": signals}

    args = resp.tool_calls[0].arguments or {}
    action = args.get("action")
    if action not in DECIDE_ACTIONS:
        return {**_fallback_decision("bad_action"), "node_key": nk,
                "title": ctx.title, "is_decision": is_decision, "signals": signals}

    note_kind = (args.get("note_kind") or "note").strip()
    conf = (args.get("confidence") or "low").strip()
    impact = (args.get("plan_impact") or "none").strip()
    sugg = args.get("suggestion")
    if isinstance(sugg, dict):
        leaning = (sugg.get("leaning") or "").strip()
        sugg = {"leaning": leaning, "why": (sugg.get("why") or "").strip()} if leaning else None
    else:
        sugg = None
    return {
        "action": action,
        "evidence": (args.get("evidence") or "").strip(),
        "error_text": (args.get("error_text") or "").strip(),
        "query": (args.get("query") or "").strip(),
        "note_text": (args.get("note_text") or "").strip(),
        "note_kind": note_kind if note_kind in _NOTE_KINDS else "note",
        "plan_impact": impact if impact in _PLAN_IMPACTS else "none",
        "suggestion": sugg,
        "confidence": conf if conf in _CONFIDENCE else "low",
        "rationale": (args.get("rationale") or "").strip()[:300],
        "node_key": nk,
        "title": ctx.title,
        "is_decision": is_decision,
        "signals": signals,
        "unavailable": False,
    }


# ── Shadow mode (Phase 1) — fire-and-forget parity capture ────────────────────
# Strong refs so tasks aren't GC'd mid-flight; tests await via drain_shadow_tasks().
_SHADOW_TASKS: set[asyncio.Task] = set()


async def drain_shadow_tasks() -> None:
    """Await all in-flight shadow decisions (test determinism)."""
    while _SHADOW_TASKS:
        await asyncio.gather(*list(_SHADOW_TASKS), return_exceptions=True)


def fire_shadow_decision(
    *, session_id: str, message: str, node_key: str | None,
    history: list[dict] | None, classifier_intent: str,
) -> None:
    """Fire-and-forget: run the unified decision on a real turn and log how it
    compares to the classifier the pipeline actually used. No-op unless the
    valve is on. Never raises into the live turn."""
    if not settings.assist_unified_decision_enabled:
        return
    task = asyncio.create_task(
        _shadow_decide_and_log(
            session_id=session_id, message=message, node_key=node_key,
            history=history, classifier_intent=classifier_intent,
        )
    )
    _SHADOW_TASKS.add(task)
    task.add_done_callback(_SHADOW_TASKS.discard)


async def _shadow_decide_and_log(
    *, session_id: str, message: str, node_key: str | None,
    history: list[dict] | None, classifier_intent: str,
) -> None:
    """Own AsyncSession — the request session is gone by the time this runs."""
    from app.database import async_session
    from app.modules import assist_agent
    try:
        async with async_session() as bg_db:
            decision = await decide_turn(
                session_id=session_id, message=message, node_key=node_key,
                history=history, db=bg_db,
            )
            if decision.get("unavailable"):
                logger.info(
                    "assist_shadow_decision session=%s UNAVAILABLE (%s)",
                    session_id, decision.get("rationale"),
                )
                return
            agree = decision["action"] == classifier_intent
            nk = decision.get("node_key")
            logger.info(
                "assist_shadow_decision session=%s node=%s agree=%s "
                "classifier=%s decide=%s conf=%s impact=%s: %s",
                session_id, nk, agree, classifier_intent, decision["action"],
                decision["confidence"], decision["plan_impact"],
                decision.get("rationale"),
            )
            # Durable record on the step's friction log, tagged for later review.
            if nk:
                note = (
                    f"[shadow §17.771] {'AGREE' if agree else 'DIFFER'} "
                    f"classifier={classifier_intent} decide={decision['action']} "
                    f"conf={decision['confidence']} impact={decision['plan_impact']}"
                    + (f" suggest={decision['suggestion']['leaning']}"
                       if decision.get("suggestion") else "")
                    + f" — {decision.get('rationale', '')}"
                    + f" | msg={json.dumps(message[:160])}"
                )
                await assist_agent.record_friction(
                    session_id=session_id, node_key=nk, note=note, db=bg_db,
                )
    except Exception as exc:  # shadow must never surface
        logger.warning("assist_shadow_decision_failed session=%s err=%r", session_id, exc)
