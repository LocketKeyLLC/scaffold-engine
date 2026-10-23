"""§17.1764 — engine code asks the operator, and reads the answer when it comes.

Live (job 2772acae T14 "Implement approve-before-run step", brief: "approve every order individually"): the engine
had no way for code to ask. The decision pause belongs to the executor -- it parks a job when its run reaches a
`decision` node -- so T14 called `park_awaiting_decision` itself, then read `frame.get("choice")` on the next line,
before anyone could answer: every order came back rejected. The review that passed it never asked when the answer
arrives. The honest shape is the one the operator already uses for every plan:

  - `open_decision_job` -- a job whose steps are the questions (one `decision` node each, fixed options, the
    payload the answer is about kept on the node), started like any run; the executor parks on the first, the
    operator answers in the UI or `POST /jobs/{id}/decide`, the run parks on the next;
  - `answer_of` -- what the operator chose, once they have: ``{"choice", "note", "approved"}``, or None while it
    is still open;
  - `answer_handler` -- an `app.*` function registered by dotted name and called with the answer the moment it
    is recorded (an approved order is placed then, not on some later poll).

A fixed decision is the operator's: the model frames nothing (the options are the code's), suggests nothing, and
"let the engine decide" is refused -- an order is never the engine's to approve.
"""
from __future__ import annotations

import importlib
import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold")

APPROVE, REJECT = "Approve", "Reject"
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_HANDLER_RE = re.compile(r"^app(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*$")


def fixed_options(node: dict | None) -> list[str]:
    """The options a code-opened decision fixes, or [] for a planner's decision (framed by the model as before)."""
    cfg = (node or {}).get("tool_config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = None
    dec = (cfg or {}).get("decision") if isinstance(cfg, dict) else None
    opts = (dec or {}).get("options") if isinstance(dec, dict) else None
    return [str(o) for o in opts if str(o).strip()] if isinstance(opts, list) else []


def fixed_frame(node: dict) -> dict:
    """The frame of a fixed decision: its own words and options, no model, no suggestion."""
    title = str(node.get("title") or "").strip()
    return {
        "question": (title.rstrip(".?") + "?") if title else "Approve?",
        "detail": str(node.get("prompt_template") or node.get("description") or "")[:4000],
        "options": [{"label": o, "fit": "", "tradeoff": ""} for o in fixed_options(node)],
        "suggested": "",
        "why": "",
        "framed": True,
        "fixed": True,
    }


def match_choice(choice: str, options: list[str]) -> str | None:
    """The option the operator's answer names (case and surrounding words ignored), or None."""
    c = (choice or "").strip().lower()
    for o in options:
        if c == o.lower():
            return o
    hits = [o for o in options if re.search(rf"\b{re.escape(o.lower())}\b", c)]
    return hits[0] if len(hits) == 1 else None


# ── handlers ────────────────────────────────────────────────────────────

def answer_handler(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Mark an ``async def fn(job_id, node_key, answer, payload)`` as callable when an answer is recorded."""
    fn.__operator_answer_handler__ = True                 # type: ignore[attr-defined]
    return fn


def _load_handler(dotted: str) -> Optional[Callable[..., Awaitable[Any]]]:
    if not _HANDLER_RE.match(dotted or ""):
        return None
    mod, _, name = dotted.partition(":")
    try:
        fn = getattr(importlib.import_module(mod), name, None)
    except ImportError:
        return None
    return fn if getattr(fn, "__operator_answer_handler__", False) else None


# ── asking ──────────────────────────────────────────────────────────────

def decision(key: str, question: str, detail: str = "", *, options: tuple[str, ...] | list[str] = (APPROVE, REJECT),
             payload: dict | None = None, depends_on: tuple[str, ...] | list[str] = ()) -> dict:
    """One question for `open_decision_job`. ``payload`` (JSON) rides on the node for the handler."""
    if not _KEY_RE.match(key):
        raise ValueError(f"decision key {key!r}: letters, digits, '_' or '-', starting with a letter")
    if len(list(options)) < 2:
        raise ValueError("a decision needs at least two options")
    return {"key": key, "question": question.strip(), "detail": detail, "options": list(options),
            "payload": payload or {}, "depends_on": list(depends_on)}


async def open_decision_job(db: AsyncSession, *, title: str, decisions: list[dict], owner: str | None = None,
                            on_answer: str | None = None, start: bool = True) -> str:
    """Open a job whose steps are ``decisions`` (from `decision`), and start its run. Returns the job id.

    ``on_answer`` -- ``"app.modules.x:fn"``, an `answer_handler` called as each answer is recorded."""
    if not decisions:
        raise ValueError("no decisions to ask")
    keys = [d["key"] for d in decisions]
    if len(set(keys)) != len(keys):
        raise ValueError("decision keys must be unique")
    if on_answer and _load_handler(on_answer) is None:
        raise ValueError(f"on_answer {on_answer!r} is not an app.* function marked @answer_handler")
    meta = {"opened_by": "operator_ask", **({"on_answer": on_answer} if on_answer else {})}
    job_id = str((await db.execute(text(
        "INSERT INTO jobs (title, input_text, status, owner, metadata) "
        "VALUES (:t, :i, 'executing', :o, CAST(:m AS jsonb)) RETURNING id"),
        {"t": title[:200], "i": title, "o": owner, "m": json.dumps(meta)})).scalar_one())
    for i, d in enumerate(decisions):
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, description, node_type, status, depends_on, tool, "
            " prompt_template, execution_order, tool_config) "
            "VALUES (:j, :k, :t, :d, 'decision', 'pending', :deps, 'LLM', :p, :o, CAST(:c AS jsonb))"),
            {"j": job_id, "k": d["key"], "t": d["question"][:300], "d": d["detail"][:2000], "deps": d["depends_on"],
             "p": d["detail"], "o": i,
             "c": json.dumps({"decision": {"options": d["options"]}, "payload": d["payload"]})})
    await db.commit()
    logger.info("operator_ask_opened job=%s decisions=%d handler=%s", job_id, len(decisions), on_answer or "-")
    if start:
        from app.modules import run_broker
        from app.modules.execution_agent import execute_all_nodes
        run_broker.start(job_id, lambda: execute_all_nodes(job_id))
    return job_id


# ── reading the answer ─────────────────────────────────────────────────

_DECIDED_RE = re.compile(r"^Decision \(operator\):\s*(.*)$", re.M)
_NOTE_RE = re.compile(r"^Note:\s*(.*)$", re.M)


async def answer_of(db: AsyncSession, job_id: str, key: str) -> dict | None:
    """``{"choice", "note", "approved"}`` once the operator has answered; None while the question is open.
    A skipped question, or one whose job was cancelled, is answered ``choice=None, approved=False``."""
    row = (await db.execute(text(
        "SELECT n.status, n.output_text, n.tool_config, j.status AS job_status FROM dag_nodes n "
        "JOIN jobs j ON j.id = n.job_id WHERE n.job_id = :j AND n.node_key = :k"),
        {"j": job_id, "k": key})).mappings().first()
    if not row:
        return None
    if row["status"] == "done":
        out = str(row["output_text"] or "")
        m = _DECIDED_RE.search(out)
        choice = m.group(1).strip() if m else ""
        note = (_NOTE_RE.search(out) or [None, ""])[1]
        picked = match_choice(choice, fixed_options(dict(row))) if fixed_options(dict(row)) else None
        return {"choice": picked or choice, "note": note, "approved": picked == APPROVE}
    if row["status"] == "skipped" or row["job_status"] in ("cancelled", "failed"):
        return {"choice": None, "note": "", "approved": False}
    return None


async def answered(db: AsyncSession, job_id: str, key: str) -> None:
    """Called by `resolve_decision` once an answer is recorded: run the job's handler, if it has one. Never raises
    -- the answer is recorded whatever the handler does."""
    try:
        row = (await db.execute(text(
            "SELECT j.metadata->>'on_answer' AS h, n.tool_config FROM jobs j JOIN dag_nodes n ON n.job_id = j.id "
            "WHERE j.id = :j AND n.node_key = :k"), {"j": job_id, "k": key})).mappings().first()
        dotted = (row or {}).get("h")
        if not dotted:
            return
        fn = _load_handler(dotted)
        if fn is None:
            logger.error("operator_ask_handler_missing job=%s node=%s handler=%s", job_id, key, dotted)
            return
        cfg = row["tool_config"] if isinstance(row["tool_config"], dict) else json.loads(row["tool_config"] or "{}")
        ans = await answer_of(db, job_id, key)
        await fn(job_id, key, ans, cfg.get("payload") or {})
    except Exception as exc:                                # noqa: BLE001 — the answer stands; the failure is logged
        logger.error("operator_ask_handler_failed job=%s node=%s err=%r", job_id, key, exc)
