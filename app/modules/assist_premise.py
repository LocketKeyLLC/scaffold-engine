"""§17.864 — step-premise verification: catch a stale step BEFORE the operator
walks into it.

The reactive correction loop (§17.677 note-impact → proposal → apply) only runs
when the operator SAYS something plan-affecting. When the direction has already
changed — facts retracted, fixes recommending a different approach — but the
plan was never revised, the claim path happily presents the next stale step
(the live home-lab failure: the tracker advanced into abandoned switch/VLAN
work). This module closes that gap at the transition moment: on step claim,
one model call judges the step's premise against the CURRENT facts ledger +
project recap. A stale verdict is surfaced to the client and (when no other
proposal is pending) staged through the same §17.677 machinery the operator
already confirms through — surface-and-ask, never auto-applied.

Gated by ``assist_step_premise_check_enabled`` (code default OFF — one extra
model call per step claim; live boxes opt in via env). Fail-soft everywhere: a
flaky check must never block claiming a step.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.config import settings
from app import model_router
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.assist")

_PREMISE_SYSTEM = (
    "You are auditing ONE step of an in-flight assisted project plan. Decide "
    "whether the step's premise still holds given the CURRENT observed facts "
    "and project recap — the facts are the truth; the step text may predate "
    "them.\n"
    "- stale=true ONLY when the facts contradict what the step assumes or "
    "sets out to do (e.g. the step configures a thing the operator has "
    "abandoned, or targets hardware the facts show cannot work). A step that "
    "is merely unstarted, hard, or differently-phrased is NOT stale.\n"
    "- When stale, propose the smallest revision that fits the current "
    "direction. If the step should simply not happen, say so.\n"
    "Call report_premise_verdict exactly once."
)

_VERDICT_TOOL = model_router.Tool(
    name="report_premise_verdict",
    description="Report whether the step's premise still holds.",
    input_schema={
        "type": "object",
        "properties": {
            "stale": {
                "type": "boolean",
                "description": "True only when current facts contradict the step's premise.",
            },
            "reason": {
                "type": "string",
                "description": "One sentence: which fact(s) contradict the step.",
            },
            "current_assumption": {
                "type": "string",
                "description": "What the step assumes that is no longer true.",
            },
            "proposed_change": {
                "type": "string",
                "description": "The smallest revision (or removal) that fits the current direction.",
            },
        },
        "required": ["stale"],
    },
)


def _facts_numbered(metadata) -> str:
    env = (metadata or {}).get("environment") if isinstance(metadata, dict) else None
    facts = (env or {}).get("facts") or []
    facts = [str(f).strip() for f in facts if str(f).strip()]
    return "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts))


async def check_step_premise(*, session_id: str, step: dict, db) -> dict | None:
    """Judge a just-claimed step against the current facts; stage a §17.677
    proposal when stale (unless one is already pending — never clobber an
    unresolved proposal with a narrower one). Returns the verdict dict for the
    claim response, or None (valve off / nothing to check / check failed)."""
    if not settings.assist_step_premise_check_enabled:
        return None
    node_key = (step or {}).get("node_key")
    if not node_key:
        return None
    try:
        sess = (await db.execute(
            text("SELECT job_id, status, metadata FROM assist_sessions WHERE id = :sid"),
            {"sid": session_id},
        )).mappings().first()
        if not sess or sess["status"] not in ("active", "paused"):
            return None
        numbered = _facts_numbered(sess.get("metadata"))
        if not numbered:
            return None  # nothing observed yet — no ground to judge against
        # §17.753 — the project recap carries cross-step decisions ("we chose
        # X over Y") that raw facts alone can miss.
        from app.modules.assist_agent import _note_impact_project_block
        recap_block = await _note_impact_project_block(str(sess["job_id"]), db)
        step_text = "\n".join(
            f"{k}: {v}" for k, v in (
                ("node_key", node_key),
                ("title", step.get("title")),
                ("description", step.get("description")),
                ("prompt", (step.get("prompt_template") or step.get("prompt") or "")[:2000]),
            ) if v
        )
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": _PREMISE_SYSTEM},
                {"role": "user", "content": (
                    f"Current observed facts (numbered):\n{numbered[:9000]}\n\n"
                    + (f"{recap_block}\n\n" if recap_block else "")
                    + f"The step about to be presented:\n{step_text}\n\n"
                    "Call report_premise_verdict."
                )},
            ],
            tools=[_VERDICT_TOOL],
            role="model_general",
            temperature=0.0,
            tool_choice="auto",
            # §17.583/727 — thinking models reason before the tool call; a
            # starved budget returns empty args.
            max_tokens=8192,
        )
        args = read_tool_args(resp) or {}
        if not args.get("stale"):
            return {"stale": False}
        verdict = {
            "stale": True,
            "reason": (args.get("reason") or "").strip()[:500],
            "proposed_change": (args.get("proposed_change") or "").strip()[:800],
            "staged": False,
        }
        # Stage through the §17.677 machinery (dismissal-suppression included)
        # unless a proposal is already awaiting the operator.
        from app.modules.assist_notes import _stage_replan_proposal, get_pending_replan
        if not await get_pending_replan(session_id=session_id, db=db):
            staged = await _stage_replan_proposal(
                session_id=session_id,
                note_text=f"Step premise check ({node_key}): {verdict['reason']}",
                note_kind="premise",
                affected=[{
                    "node_key": node_key,
                    "action": "revise",
                    "current_assumption": (args.get("current_assumption") or "").strip()[:500],
                    "proposed_change": verdict["proposed_change"],
                }],
                db=db,
            )
            verdict["staged"] = bool(staged)
        logger.info(
            "assist_step_premise_stale session_id=%s node_key=%s staged=%s reason=%s",
            session_id, node_key, verdict["staged"], verdict["reason"][:160],
        )
        return verdict
    except Exception as exc:  # noqa: BLE001 — never block a step claim
        logger.warning(
            "assist_step_premise_check_failed session_id=%s node_key=%s err=%r",
            session_id, node_key, exc,
        )
        return None
