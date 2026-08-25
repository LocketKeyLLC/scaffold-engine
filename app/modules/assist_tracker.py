"""§17.754 — the progress-tracking agent (operator-directed).

The recurring failure: the session pointer (``current_node_key``) is where the
plan THINKS the operator is, but the operator moves faster than the plan — they
finish a step without submitting it, or they start a concrete sub-task the plan
has no step for (e.g. "set up the network on the installed server" while the
pointer still says "install guest OS"). A help request then gets answered against
the stale step, so the engine "repeats itself" instead of helping.

The deterministic recap/facts (§17.738/752) DESCRIBE state but never RECONCILE the
pointer with reality. This is that reconciler: an LLM agent that, on a substantive
turn, reads where the operator actually is against the DAG and returns an action —
``on_step`` (proceed), ``advance`` (current step is effectively done), or
``add_step`` (a real sub-task no existing step covers — insert one and walk them
through it via the §17.736 machinery). Deterministic guardrails: valve-gated,
confidence-thresholded, fail-soft to ``on_step`` so a flaky agent never traps the
turn or mutates the plan on a guess.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.providers.base import Tool
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold")


TRACK_PROGRESS_TOOL = Tool(
    name="report_progress",
    description=(
        "Report where the operator ACTUALLY is relative to the plan's current "
        "step, and the single best action to keep the guided plan in sync with "
        "reality."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["on_step", "advance", "add_step"],
                "description": (
                    "'on_step' — the operator is still working the CURRENT step; "
                    "proceed normally. 'advance' — the current step is effectively "
                    "DONE (the operator has moved past it) and the next work is an "
                    "existing pending step. 'add_step' — the operator is now doing "
                    "a concrete sub-task that NO existing step (pending or done) "
                    "covers, so a new guided step must be inserted and walked "
                    "through."
                ),
            },
            "current_step_done": {
                "type": "boolean",
                "description": (
                    "True if the CURRENT step's goal has effectively been achieved "
                    "already (the operator finished it, even if they never formally "
                    "submitted it)."
                ),
            },
            "covered_by_node": {
                "type": "string",
                "description": (
                    "If what the operator is now doing IS already covered by an "
                    "existing step, that step's node_key (so we advance to it "
                    "instead of adding a duplicate). Empty string if none covers it."
                ),
            },
            "new_step_title": {
                "type": "string",
                "description": (
                    "For verdict='add_step': a short imperative title for the "
                    "missing step (e.g. 'Configure guest network on the installed "
                    "Ubuntu server'). Empty otherwise."
                ),
            },
            "new_step_request": {
                "type": "string",
                "description": (
                    "For verdict='add_step': a concrete one-sentence request "
                    "describing what the new guided step must accomplish, phrased "
                    "so it can be drafted into a walkthrough (e.g. 'add a step to "
                    "configure networking on the freshly installed Ubuntu guest so "
                    "it has a working IP and internet access'). Empty otherwise."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "0.0-1.0 confidence in this verdict.",
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining the verdict.",
            },
        },
        "required": ["verdict"],
    },
)


_TRACKER_SYSTEM = (
    "You are the progress tracker for a hands-on, step-by-step build that a human "
    "operator is doing with an assistant. The plan is a fixed list of steps; the "
    "engine currently believes the operator is on ONE particular step. Operators "
    "often move faster than the plan — they finish a step without formally "
    "submitting it, or they start a concrete sub-task the plan never had a step "
    "for. Your ONE job: compare where the operator ACTUALLY is (from their latest "
    "message, the running recap, and the recent dialogue) against the plan, and "
    "call report_progress with the single best action to re-sync the plan.\n\n"
    "Decide:\n"
    "- add_step — the operator is asking for help with, or actively doing, a "
    "concrete task that NONE of the listed steps (pending or done) covers. This is "
    "the important case: give a new_step_title + new_step_request so we insert a "
    "guided step and walk them through it instead of repeating the current step. "
    "Example: pointer says 'Install guest OS' but the operator has finished "
    "installing and now needs to configure networking on the running server, and "
    "no step covers guest networking.\n"
    "- advance — the current step is clearly finished AND the very next thing the "
    "operator needs is an existing pending step (name it in covered_by_node).\n"
    "- on_step — the operator is still working the current step (a question, a "
    "hiccup, a refinement about THIS step). This is the safe default.\n\n"
    "Be conservative: only choose add_step when you are genuinely confident a real "
    "sub-task is uncovered — a passing question about the current step is on_step, "
    "not a new step. Never invent a sub-task the operator did not raise. When "
    "unsure, choose on_step with lower confidence."
)


async def assess_progress(*, session_id: str, message: str, db,
                          node_key: str | None = None,
                          history: list[dict] | None = None) -> dict:
    """§17.754 — reconcile the plan pointer with where the operator actually is.

    Returns ``{verdict, node_key, current_step_done, covered_by_node,
    new_step_title, new_step_request, confidence, reason}`` — ``node_key`` is
    the step the tracker actually assessed (§17.812, audit I-3/M14: the caller's
    step when it names a real node, else the session pointer), so the caller
    retires the step DISCUSSED, not whatever the pointer drifted to cross-chat.
    ``history`` (same-chat OWUI turns) grounds the judgment when supplied; the
    durable transcript is the fallback. Fail-soft: a disabled valve, a
    non-active session, a model error, or an unparsed tool call all return
    ``{verdict: 'on_step', confidence: 0.0, reason: <why>}`` so the caller simply
    proceeds with normal handling — the tracker never blocks or mutates on doubt.
    """
    from app.config import settings
    from app.modules import assist_agent, assist_guide

    _NOOP = {"verdict": "on_step", "current_step_done": False,
             "covered_by_node": "", "new_step_title": "", "new_step_request": "",
             "confidence": 0.0, "reason": ""}
    if not settings.assist_progress_tracker_enabled:
        return {**_NOOP, "reason": "tracker_disabled"}
    try:
        sess = (await db.execute(
            text("SELECT job_id, status, current_node_key, metadata "
                 "FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess or sess["status"] not in ("active", "paused"):
            return {**_NOOP, "reason": "session_not_active"}
        job_id = str(sess["job_id"])
        nk = sess["current_node_key"]
        nodes = (await db.execute(
            text("SELECT node_key, title, status FROM dag_nodes WHERE job_id = :jid "
                 "ORDER BY execution_order NULLS LAST, node_key"),
            {"jid": job_id},
        )).mappings().all()
        # §17.812 (audit I-3/M14) — ground on the step the caller is actually
        # discussing when it names a real node; unknown/stale keys fall back to
        # the session pointer so a bad caller hint can't derail the assessment.
        if node_key and any(n["node_key"] == node_key for n in nodes):
            nk = node_key
        if not nk or not nodes:
            return {**_NOOP, "reason": "no_current_step"}
        cur = next((n for n in nodes if n["node_key"] == nk), None)
        cur_title = (cur or {}).get("title") or nk
        steps_block = "\n".join(
            f"- {n['node_key']} ({n['status']}): {n['title'] or n['node_key']}"
            + ("  <-- CURRENT" if n["node_key"] == nk else "")
            for n in nodes
        )
        recap = await assist_agent.get_step_recap(
            session_id=session_id, node_key=nk, title=cur_title, db=db)
        facts = assist_guide.render_facts_block(
            assist_agent._environment_from_metadata(sess.get("metadata")))
        history = await assist_agent._history_or_transcript(
            history=history, session_id=session_id, db=db, exclude_tail=message)
        convo = assist_agent._conversation_block_for(history)

        user = (
            f"The engine currently believes the operator is on step {nk}: "
            f"\"{cur_title}\".\n\n"
            f"The operator just said:\n\"{(message or '').strip()[:1500]}\"\n\n"
            f"All plan steps and their status:\n{steps_block[:4000]}\n\n"
            + (f"Running recap of the current step:\n{recap[:2000]}\n\n" if recap else "")
            + (f"{facts[:1500]}\n\n" if facts else "")
            + (f"Recent dialogue:\n{convo[:3000]}\n\n" if convo else "")
            + "Call report_progress."
        )
        from app import model_router
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _TRACKER_SYSTEM},
                {"role": "user", "content": user},
            ],
            tools=[TRACK_PROGRESS_TOOL],
            role="model_general",   # reasoning task; the verifier false-negatives (§17.677)
            temperature=0.0,
            tool_choice="auto",
            max_tokens=2048,        # thinking model reasons before the tool call
        )
    except Exception as e:  # noqa: BLE001 — a flaky tracker must never trap the turn
        logger.warning("assist_progress_tracker_failed session_id=%s err=%r", session_id, e)
        return {**_NOOP, "reason": "tracker_unavailable"}
    args = read_tool_args(resp)
    if not args or args.get("verdict") not in ("on_step", "advance", "add_step"):
        logger.warning("assist_progress_tracker_unparsed session_id=%s", session_id)
        return {**_NOOP, "reason": "tracker_unparsed"}
    out = {**_NOOP}
    out.update({
        "node_key": nk,   # §17.812 — the step this verdict is ABOUT
        "verdict": args["verdict"],
        "current_step_done": bool(args.get("current_step_done")),
        "covered_by_node": str(args.get("covered_by_node") or "").strip(),
        "new_step_title": str(args.get("new_step_title") or "").strip(),
        "new_step_request": str(args.get("new_step_request") or "").strip(),
        "confidence": float(args.get("confidence") or 0.0),
        "reason": str(args.get("reason") or "")[:300],
    })
    logger.info(
        "assist_progress_tracker session_id=%s verdict=%s conf=%.2f covered=%s",
        session_id, out["verdict"], out["confidence"], out["covered_by_node"] or "-",
    )
    return out
