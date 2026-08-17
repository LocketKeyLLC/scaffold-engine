"""NL command handlers for the native surface (§17.790).

Classifies a plain message via :func:`app.modules.command_guide.classify_command`
and, for high-confidence engine actions, executes them in-process (reads, and
confirm-gated writes/deletes), rendering the result as chat text. Anything not
handled — or below ``high`` confidence — returns None so the caller falls through
to triage / the model passthrough.

Each handled intent has an executor in :data:`_RENDER` (an async generator of text
pieces). Confirm-gated intents (:data:`_CONFIRM_INTENTS`) additionally have a card
builder: on classification they emit a confirm card, and the executor runs only
after an affirmative follow-up (see ``dispatch.route`` + ``confirm_cards``).
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Callable

from app.modules import command_guide
from app.native_chat import confirm_cards, engine_client as ec, renderers

logger = logging.getLogger("scaffold.native_chat")

# Intents handled natively today (Phase 2). The rest fall through to the
# passthrough/triage — tracked in docs/native_openai_surface_plan.md.
_CONFIRM_INTENTS = frozenset({"jobs_delete", "research_topic"})

# Required slots per intent; a high-confidence hit missing one gets a clarify
# rather than a silent misfire.
_REQUIRED_SLOTS: dict[str, list[str]] = {
    "jobs_find": ["query"],
    "rag_query": ["query"],
    "research_topic": ["topic"],
    "jobs_delete": ["target_ref"],
}


def _err(name: str, code: int, body: Any) -> str:
    detail = body.get("detail") if isinstance(body, dict) else body
    return f"Couldn't fetch {name} (HTTP {code}): {detail}"


async def _resolve_job(ref: str) -> tuple[str, str] | None:
    """Resolve a job reference (id prefix or title fragment) to ``(id, title)``.

    Empty ref → the most recent job. Newest-first (the /jobs endpoint orders by
    recency), so a title fragment matches the most recent job with that title.
    """
    code, body = await ec.get_json("/jobs", params={"limit": 50})
    jobs = (body.get("jobs") if isinstance(body, dict) else None) or []
    if code != 200 or not jobs:
        return None
    ref = (ref or "").strip()
    if not ref:
        j = jobs[0]
        return j.get("id", ""), j.get("title", "")
    low = ref.lower()
    for j in jobs:  # id prefix
        jid = (j.get("id") or "").lower()
        if jid.startswith(low) or (len(low) >= 8 and low.startswith(jid[:8])):
            return j.get("id", ""), j.get("title", "")
    for j in jobs:  # title fragment
        if low in (j.get("title") or "").lower():
            return j.get("id", ""), j.get("title", "")
    return None


# ── Executors (async generators of text) ──────────────────────────────────────
async def _run_status(slots: dict) -> AsyncIterator[str]:
    code, body = await ec.get_json("/status")
    yield renderers.status(body) if code == 200 else _err("status", code, body)


async def _run_health(slots: dict) -> AsyncIterator[str]:
    code, body = await ec.get_json("/health")
    yield renderers.health(body) if code == 200 else _err("health", code, body)


async def _run_config(slots: dict) -> AsyncIterator[str]:
    code, body = await ec.get_json("/config")
    yield renderers.config(body) if code == 200 else _err("config", code, body)


async def _run_jobs_list(slots: dict) -> AsyncIterator[str]:
    code, body = await ec.get_json("/jobs", params={"limit": 12})
    yield renderers.jobs_list(body) if code == 200 else _err("jobs", code, body)


async def _run_jobs_find(slots: dict) -> AsyncIterator[str]:
    query = slots.get("query", "")
    code, body = await ec.get_json("/jobs", params={"limit": 50})
    if code != 200 or not isinstance(body, dict):
        yield _err("jobs", code, body)
        return
    low = query.lower()
    matches = [j for j in (body.get("jobs") or []) if low in (j.get("title") or "").lower()]
    yield renderers.jobs_list({"jobs": matches, "total": len(matches)}, header=f"Jobs matching '{query}'")


async def _run_rag(slots: dict) -> AsyncIterator[str]:
    query = slots.get("query", "")
    code, body = await ec.request_json("POST", "/rag", json={"query": query})
    yield renderers.rag(body) if code == 200 else _err("knowledge base", code, body)


async def _run_results(slots: dict) -> AsyncIterator[str]:
    job = await _resolve_job(slots.get("job_ref", ""))
    if not job:
        yield "I couldn't find that job. Try \"show my jobs\"."
        return
    code, body = await ec.get_json(f"/exec/status/{job[0]}")
    yield renderers.results(body) if code == 200 else _err("results", code, body)


async def _run_logs(slots: dict) -> AsyncIterator[str]:
    job = await _resolve_job(slots.get("job_ref", ""))
    if not job:
        yield "I couldn't find that job. Try \"show my jobs\"."
        return
    code, body = await ec.get_json(f"/logs/{job[0]}", params={"limit": 100})
    yield renderers.logs(body) if code == 200 else _err("logs", code, body)


async def _run_cost(slots: dict) -> AsyncIterator[str]:
    job = await _resolve_job(slots.get("job_ref", ""))
    if not job:
        yield "I couldn't find that job. Try \"show my jobs\"."
        return
    code, body = await ec.get_json(f"/jobs/{job[0]}/costs")
    yield renderers.cost(body) if code == 200 else _err("cost", code, body)


async def _run_help(slots: dict) -> AsyncIterator[str]:
    yield renderers.HELP


async def _run_jobs_delete(slots: dict) -> AsyncIterator[str]:
    # The card resolved the target and stored the concrete id/title, so commit is
    # unambiguous. Fall back to re-resolving target_ref if a bare slot arrives.
    job_id = slots.get("_job_id")
    title = slots.get("_job_title", "")
    if not job_id:
        job = await _resolve_job(slots.get("target_ref", ""))
        if not job:
            yield "I couldn't find that job to delete."
            return
        job_id, title = job
    code, body = await ec.request_json("DELETE", f"/jobs/{job_id}")
    yield renderers.delete_result("job", title or job_id[:8], code, body)


_RESEARCH_EVENT_LABELS = {
    "research_started": "🔎 Research started",
    "decomposition_complete": "Decomposed the topic into facets",
    "iteration_started": "Searching",
    "search_complete": "Search complete",
    "extraction_complete": "Extracted sources",
    "ingestion_complete": "Ingested findings",
    "gap_analysis": "Assessing coverage",
    "convergence": "Converged",
    "awaiting_reply": "Paused — needs a clarification",
}


async def _run_research_topic(slots: dict) -> AsyncIterator[str]:
    topic = slots.get("topic", "")
    depth = slots.get("depth") or "medium"
    yield f"🔎 Starting research on **{topic}** (depth: {depth})…\n"
    async for event, data in ec.stream_sse("/research", json={"topic": topic, "depth": depth}):
        if event == "research_complete":
            summary = data.get("summary") if isinstance(data, dict) else None
            if summary:
                yield "\n\n" + str(summary).strip()
            return
        if event == "error":
            detail = data.get("detail") if isinstance(data, dict) else data
            yield f"\n\nResearch error: {detail}"
            return
        label = _RESEARCH_EVENT_LABELS.get(event)
        if label:
            yield f"\n- {label}"


# ── Confirm-card builders (async generators) ──────────────────────────────────
async def _card_jobs_delete(slots: dict) -> AsyncIterator[str]:
    job = await _resolve_job(slots.get("target_ref", ""))
    if not job:
        yield f"I couldn't find a job matching \"{slots.get('target_ref', '')}\" to delete."
        return
    job_id, title = job
    stored = {**slots, "_job_id": job_id, "_job_title": title}
    human = (
        f"You asked to delete **{title or job_id[:8]}** (`{job_id[:8]}`). "
        "This removes the job and its nodes/logs. Reply **yes** to delete, or **no** to cancel."
    )
    yield confirm_cards.render_card("jobs_delete", stored, human)


async def _card_research_topic(slots: dict) -> AsyncIterator[str]:
    topic = slots.get("topic", "")
    depth = slots.get("depth") or "medium"
    human = (
        f"Run autonomous web research on **{topic}** (depth: {depth})? "
        "This can take 20–60 minutes on CPU. Reply **yes** to start, or **no** to cancel."
    )
    yield confirm_cards.render_card("research_topic", slots, human)


_RENDER: dict[str, Callable[[dict], AsyncIterator[str]]] = {
    "status": _run_status,
    "health": _run_health,
    "config": _run_config,
    "jobs_list": _run_jobs_list,
    "jobs_find": _run_jobs_find,
    "rag_query": _run_rag,
    "results": _run_results,
    "logs": _run_logs,
    "cost": _run_cost,
    "help": _run_help,
    "jobs_delete": _run_jobs_delete,
    "research_topic": _run_research_topic,
}
_CARD: dict[str, Callable[[dict], AsyncIterator[str]]] = {
    "jobs_delete": _card_jobs_delete,
    "research_topic": _card_research_topic,
}
HANDLED_INTENTS = frozenset(_RENDER)


async def _clarify(intent: str, missing: list[str]) -> AsyncIterator[str]:
    friendly = {
        "query": "what to search for",
        "topic": "the research topic",
        "target_ref": "which one to delete",
    }
    need = ", ".join(friendly.get(m, m) for m in missing)
    yield f"Sure — I just need {need}."


async def classify_and_dispatch(user_text: str) -> AsyncIterator[str] | None:
    """Classify a plain message and return an executor generator, or None.

    None means "not a high-confidence handled command" → the caller falls
    through to triage / the model passthrough.
    """
    result = await command_guide.classify_command(message=user_text)
    intent = result.get("intent")
    if intent not in HANDLED_INTENTS or result.get("confidence") != "high":
        return None
    missing = [s for s in _REQUIRED_SLOTS.get(intent, []) if not result.get(s)]
    if missing:
        return _clarify(intent, missing)
    if intent in _CONFIRM_INTENTS:
        return _CARD[intent](result)
    return _RENDER[intent](result)


def commit(pending: dict[str, Any]) -> AsyncIterator[str] | None:
    """Execute a previously-confirmed action (affirmative follow-up)."""
    intent = pending.get("intent")
    fn = _RENDER.get(intent)
    if fn is None:
        return None
    return fn(pending.get("slots") or {})
