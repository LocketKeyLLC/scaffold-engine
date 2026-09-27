"""§17.1184 — the autonomous run stops to ASK at a decision step.

The plan marks the choices that are the operator's to make (`node_type =
'decision'`: "Decide P40 GPU passthrough strategy — whole-GPU vs Docker vs
LXC"), and the approve page promises that a question left blank "becomes an
explicit decision point that pauses the run and asks you". Assist mode kept
that promise (§17.654/689 — one choice at a time, suggest, never resolve).
Auto mode did not: the executor prompted the LLM with the decision step like
any other and the model picked for the operator — the review of the live
home-lab job found no runtime stop of any kind.

Now the executor, before it claims a decision node, parks the job in
``awaiting_decision`` with the question framed for the operator (the options
the plan names, a suggestion marked "your call"), emits ``awaiting_decision``,
and returns. ``POST /jobs/{id}/decide`` records the choice as the node's
output — downstream steps read it as upstream context, exactly as if the
model had produced it — and restarts the detached run. "Let the engine
decide" is explicit: the node is marked delegated and the next run executes
it the old way.

Fail-soft on the framing call only: if the model cannot frame the question,
the operator gets the step's own text and a free-text answer. The PAUSE is
not fail-soft — a decision the engine could not frame is still not the
engine's to make.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.modules.job_state import transition

logger = logging.getLogger("scaffold")

STATUS = "awaiting_decision"
MAX_OPTIONS = 5


# ── what is waiting ─────────────────────────────────────────────────────

async def delegated_decisions(db: AsyncSession, job_id: str) -> list[str]:
    """Node keys the operator handed back to the engine ("you decide")."""
    row = (await db.execute(
        text("SELECT metadata->'decisions' FROM jobs WHERE id = :jid"), {"jid": job_id},
    )).scalar()
    decisions = _as_dict(row)
    return [k for k, v in decisions.items() if isinstance(v, dict) and v.get("by") == "engine"]


async def pending_decision(db: AsyncSession, job_id: str) -> dict | None:
    """The first dep-satisfied pending decision node the operator has not
    delegated — the same NOT EXISTS shape as ``_claim_ready_nodes`` so the
    pause fires exactly where a claim would have."""
    delegated = await delegated_decisions(db, job_id)
    row = (await db.execute(
        text("""
            SELECT n.node_key, n.title, n.description, n.prompt_template, n.depends_on
            FROM dag_nodes n
            WHERE n.job_id = :jid AND n.status = 'pending' AND n.node_type = 'decision'
              AND NOT (n.node_key = ANY(:delegated))
              AND NOT EXISTS (
                  SELECT 1
                  FROM unnest(COALESCE(n.depends_on, ARRAY[]::text[])) AS dep(k)
                  WHERE NOT EXISTS (
                      SELECT 1 FROM dag_nodes d
                      WHERE d.job_id = :jid AND d.node_key = dep.k
                        AND d.status IN ('done', 'skipped')
                  )
              )
            ORDER BY n.execution_order ASC
            LIMIT 1
        """),
        {"jid": job_id, "delegated": delegated},
    )).mappings().first()
    return dict(row) if row else None


# ── framing the question ────────────────────────────────────────────────

def _frame_tool():
    from app.providers.base import Tool
    return Tool(
        name="frame_decision",
        description=(
            "Put ONE decision to the person who owns the outcome. State the question in "
            "plain words, list the options the step names (add one only if the step "
            "clearly implies it), give each a fit and a trade-off, and suggest one — "
            "the choice stays theirs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The one question, in plain words, ending with '?'."},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "Short name for the option"},
                            "fit": {"type": "string", "description": "When it is the right pick"},
                            "tradeoff": {"type": "string", "description": "What it costs"},
                        },
                        "required": ["label", "fit", "tradeoff"],
                    },
                },
                "suggested": {"type": "string", "description": "The label you would lean toward (must match one above)."},
                "why": {"type": "string", "description": "One line on why."},
            },
            "required": ["question", "options"],
        },
    )


_FRAME_SYSTEM = (
    "You frame a decision for the person whose system this is. They will answer in one "
    "line and the plan continues on their answer. Use ONLY the step text, the brief and "
    "the completed steps below — no options from outside them. Never decide for them."
)


def fallback_frame(node: dict) -> dict:
    """What the operator sees when the model could not frame the step."""
    title = str(node.get("title") or "").strip()
    body = str(node.get("prompt_template") or node.get("description") or "").strip()
    return {
        "question": (title.rstrip(".?") + "?") if title else "What do you want here?",
        "detail": body[:1200],
        "options": [],
        "suggested": "",
        "why": "",
        "framed": False,
    }


async def frame_decision(node: dict, *, brief: str = "", upstream: str = "") -> dict:
    """``{question, detail, options[{label,fit,tradeoff}], suggested, why, framed}``.
    Fail-soft to ``fallback_frame`` — the pause happens either way."""
    from app import model_router
    from app.utils.tool_call_args import read_tool_args

    fb = fallback_frame(node)
    prompt = (
        f"Step: {node.get('title') or ''}\n"
        f"Step text: {str(node.get('prompt_template') or node.get('description') or '')[:2000]}\n\n"
        + (f"Brief: {brief[:1200]}\n\n" if brief else "")
        + (f"Completed steps:\n{upstream[:3000]}\n\n" if upstream else "")
        + "Call frame_decision."
    )
    try:
        resp = await model_router.tool_call(
            messages=[{"role": "system", "content": _FRAME_SYSTEM}, {"role": "user", "content": prompt}],
            tools=[_frame_tool()], role="model_general", temperature=0.2, max_tokens=1536,
        )
        parsed = read_tool_args(resp)
    except Exception as exc:  # the pause must not depend on the model
        logger.warning("decision_frame_failed node=%s err=%s", node.get("node_key"), exc)
        return fb
    if not parsed or not str(parsed.get("question") or "").strip():
        return fb
    opts = [
        {"label": str(o.get("label", "")).strip(), "fit": str(o.get("fit", "")).strip(),
         "tradeoff": str(o.get("tradeoff", "")).strip()}
        for o in (parsed.get("options") or []) if isinstance(o, dict) and str(o.get("label", "")).strip()
    ][:MAX_OPTIONS]
    labels = {o["label"] for o in opts}
    suggested = str(parsed.get("suggested") or "").strip()
    if suggested not in labels:
        suggested = ""
    return {
        "question": str(parsed["question"]).strip()[:600],
        "detail": fb["detail"],
        "options": opts,
        "suggested": suggested,
        "why": str(parsed.get("why") or "").strip()[:400] if suggested else "",
        "framed": True,
    }


# ── park / resolve ──────────────────────────────────────────────────────

async def park_awaiting_decision(db: AsyncSession, job_id: str, node: dict, frame: dict) -> dict:
    """Write the question on the job, move it to ``awaiting_decision`` and
    return the SSE payload. The node stays ``pending`` — nothing was claimed."""
    asked = {
        "node_key": node["node_key"], "title": node.get("title") or "",
        "asked_at": datetime.now(timezone.utc).isoformat(), **frame,
    }
    await db.execute(
        text("UPDATE jobs SET metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb) WHERE id = :jid"),
        {"jid": job_id, "patch": json.dumps({"awaiting_decision": asked})},
    )
    ok = await transition(db, job_id, to=STATUS, expected_from=("running", "executing"),
                          reason=f"decision:{node['node_key']}")
    await db.commit()
    logger.info("decision_pause_parked job=%s node=%s framed=%s options=%d transitioned=%s",
                job_id, node["node_key"], frame.get("framed"), len(frame.get("options") or []), ok)
    return {"job_id": job_id, "status": STATUS, **asked}


def decision_record(choice: str, note: str, frame: dict | None) -> str:
    """The node's output — what downstream steps read as the decision."""
    lines = [f"Decision (operator): {choice.strip()}"]
    if note and note.strip():
        lines.append(f"Note: {note.strip()}")
    opts = (frame or {}).get("options") or []
    if opts:
        lines.append("Options considered: " + "; ".join(o.get("label", "") for o in opts if o.get("label")))
    return "\n".join(lines)


async def resolve_decision(
    db: AsyncSession, job_id: str, node_key: str, *, choice: str | None, note: str = "", delegate: bool = False,
    inputs: dict | None = None,
) -> dict:
    """Record the operator's answer (or their delegation) and put the job back
    to ``executing`` so the run can be restarted. Returns ``{"outcome": …}``:
    ``resolved`` | ``delegated`` | ``not_waiting`` (nothing pending, or a
    different node) | ``node_gone`` (the node is no longer pending)."""
    job = (await db.execute(
        text("SELECT status, metadata FROM jobs WHERE id = :jid FOR UPDATE"), {"jid": job_id},
    )).mappings().first()
    if not job:
        return {"outcome": "not_found"}
    md = _as_dict(job.get("metadata"))
    waiting = md.get("awaiting_decision") if isinstance(md.get("awaiting_decision"), dict) else {}
    if job["status"] != STATUS or waiting.get("node_key") != node_key:
        return {"outcome": "not_waiting", "current_status": job["status"], "waiting_on": waiting.get("node_key")}
    now = datetime.now(timezone.utc).isoformat()
    if waiting.get("kind") == "run":
        # §17.1186 — a hands-on step parked with its commands: run / myself / skip
        from app.modules import supervised_runs
        res = await supervised_runs.resolve_run(db, job_id, node_key, ("myself" if delegate else (choice or "")), waiting,
                                                inputs=inputs)
        if res["outcome"] in ("bad_choice", "not_runnable", "no_channel", "node_gone", "inputs_missing"):
            await db.rollback()
            return {"outcome": res["outcome"], "current_status": job["status"], "waiting_on": node_key, **{k: v for k, v in res.items() if k != "outcome"}}
        entry = {"by": "operator", "choice": ("myself" if delegate else choice), "note": (note or "").strip(),
                 "at": now, "result": res["outcome"], "node_status": res.get("node_status"),
                 "inputs": sorted((inputs or {}).keys())}       # names only — a value may be a secret
        outcome = res["outcome"]
    elif delegate:
        entry = {"by": "engine", "at": now}
        outcome = "delegated"
    else:
        rec = decision_record(choice or "", note, waiting)
        upd = await db.execute(
            text("UPDATE dag_nodes SET status = 'done', output_text = :out, completed_at = NOW(), "
                 "started_at = COALESCE(started_at, NOW()), updated_at = NOW() "
                 "WHERE job_id = :jid AND node_key = :nk AND status = 'pending'"),
            {"jid": job_id, "nk": node_key, "out": rec},
        )
        if upd.rowcount == 0:
            return {"outcome": "node_gone"}
        entry = {"by": "operator", "choice": (choice or "").strip(), "note": (note or "").strip(), "at": now}
        outcome = "resolved"
    decisions = _as_dict(md.get("decisions"))
    decisions[node_key] = entry
    await db.execute(
        text("UPDATE jobs SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'awaiting_decision') "
             "|| CAST(:patch AS jsonb) WHERE id = :jid"),
        {"jid": job_id, "patch": json.dumps({"decisions": decisions})},
    )
    await transition(db, job_id, to="executing", expected_from=(STATUS,), reason=f"decided:{node_key}")
    await db.commit()
    logger.info("decision_pause_%s job=%s node=%s", outcome, job_id, node_key)
    return {"outcome": outcome, "node_key": node_key, "record": entry}


def _as_dict(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            return {}
    return dict(v) if isinstance(v, dict) else {}


def enabled() -> bool:
    return bool(settings.decision_pause_enabled)
