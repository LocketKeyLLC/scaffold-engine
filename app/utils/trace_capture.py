"""§17.786 — full request/response trace capture for LLM calls.

Companion to :mod:`app.utils.cost_tracking`. Where ``record_llm_call`` records
the *metrics* of every LLM call (tokens, latency, USD cost) into
``llm_call_logs``, this module records the *content* — the prompt or serialized
messages, the system prompt, sampling params, the response text, any tool
calls, and the error — into the ``llm_traces`` table. The two rows share the
same job/node/call_kind association keys (reused verbatim from
``cost_tracking``), so a trace JOINs 1:1 against its metrics row.

Flow, mirroring the cost path:

1. **Request snapshot ContextVar.** ``model_router``'s public entry points
   (``generate``/``chat``/``tool_call``/``embed``) call :func:`set_current_request`
   on entry, stashing a lightweight snapshot (references, not copies) of the
   request in the ``current_request`` ContextVar. Nested/fallback calls within
   the same logical request re-read the outer snapshot — correct, since they
   belong to that request.

2. **``record_trace(resp)``** — async fire-and-forget hook invoked from
   ``model_router._record_call`` (the single post-call chokepoint) alongside
   ``record_llm_call``. Reads the request snapshot + job/node/call_kind
   ContextVars, truncates each content field to ``trace_capture_max_chars``, and
   INSERTs one ``llm_traces`` row using its own short-lived DB session.

The whole feature is gated behind the default-OFF ``trace_capture_enabled``
valve: when off, both :func:`set_current_request` and :func:`record_trace`
short-circuit to a no-op so there's zero overhead and no content is stored.
Every failure path is swallowed at WARNING/DEBUG — trace capture must never
break the LLM call path.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any, Optional

from sqlalchemy import text

# Reuse the SAME association ContextVars the cost path uses so a trace and its
# llm_call_logs metrics row are tagged identically (they JOIN on these).
from app.utils.cost_tracking import (
    current_call_kind,
    current_job_id,
    current_node_id,
)

logger = logging.getLogger("scaffold.trace_capture")

# Carries a snapshot of the in-flight request from the model_router entry point
# down to record_trace, without threading it through every call site as a kwarg.
# The snapshot is a dict: {kind, prompt, messages, system, temperature,
# max_tokens}. Default None → record_trace writes NULL content (an off-router
# caller, or the valve was off at request time).
current_request: ContextVar[Optional[dict]] = ContextVar(
    "scaffold_current_request", default=None,
)


def set_current_request(
    kind: str,
    *,
    prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> None:
    """Stash a snapshot of the in-flight LLM request for the trace writer.

    Called at the top of each ``model_router`` public entry point. No-op when
    ``trace_capture_enabled`` is off so the default path pays nothing. Stores
    references (not deep copies) — serialization + truncation happen lazily in
    :func:`record_trace`, so this stays O(1) regardless of prompt size.

    Never raises: a bad settings read or import must not break dispatch.
    """
    try:
        from app.config import settings
        if not settings.trace_capture_enabled:
            return
        current_request.set({
            "kind": kind,
            "prompt": prompt,
            "messages": messages,
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
    except Exception:
        logger.debug("set_current_request_escape", exc_info=True)


def _truncate(value: str | None, limit: int) -> str | None:
    """Truncate ``value`` to ``limit`` chars, marking how many were dropped.

    None passes through; a string within the limit is returned unchanged; an
    over-length string is cut and suffixed with ``…[+N chars]`` so a reader
    knows the trace is partial. ``limit`` is clamped to >= 1 defensively.
    """
    if value is None:
        return None
    limit = max(1, limit)
    if len(value) <= limit:
        return value
    dropped = len(value) - limit
    return f"{value[:limit]}…[+{dropped} chars]"


def _serialize_request(snapshot: dict | None) -> str | None:
    """Render the request content as text: the prompt, or JSON-serialized
    messages (chat/tool_call), or the joined embed inputs. None when no
    snapshot was captured."""
    if not snapshot:
        return None
    messages = snapshot.get("messages")
    if messages is not None:
        try:
            return json.dumps(messages, ensure_ascii=False, default=str)
        except Exception:
            return str(messages)
    prompt = snapshot.get("prompt")
    if prompt is not None:
        return prompt if isinstance(prompt, str) else str(prompt)
    return None


def _serialize_tool_calls(resp) -> str | None:
    """Serialize ``resp.tool_calls`` to a JSON string, or None when the model
    invoked no tools. Each ToolCall is a dataclass — read its fields defensively
    so a shape change can't raise."""
    calls = getattr(resp, "tool_calls", None) or []
    if not calls:
        return None
    try:
        payload = [
            {
                "id": getattr(c, "id", None),
                "name": getattr(c, "name", None),
                "arguments": getattr(c, "arguments", None),
            }
            for c in calls
        ]
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return None


async def record_trace(resp) -> None:
    """Insert one ``llm_traces`` row for a completed LLM call.

    No-op unless ``trace_capture_enabled`` is on. ``resp`` is a
    ``ModelResponse``-shaped object (provider, model, text, tokens, latency,
    success, error, tool_calls); the request content comes from the
    ``current_request`` snapshot set by the caller. Runs in its own short-lived
    async session so it can't deadlock a caller holding a session open.

    Failures are swallowed at WARNING — trace capture must never break the LLM
    call path. If the ``llm_traces`` table is missing (test env without the 063
    migration), the warning fires and subsequent calls silently continue.
    """
    try:
        from app.config import settings
        if not settings.trace_capture_enabled:
            return
        limit = int(getattr(settings, "trace_capture_max_chars", 8000))
    except Exception:
        return

    try:
        from app.database import async_session
    except Exception:
        return  # database wiring unavailable — silently skip

    snapshot = current_request.get()
    request_kind = (snapshot or {}).get("kind") or "unknown"

    provider = (getattr(resp, "provider", "") or "").strip() or "unknown"
    model = (getattr(resp, "model", "") or "").strip() or "unknown"

    params = {
        "job_id": current_job_id.get(),
        "node_id": current_node_id.get(),
        "call_kind": current_call_kind.get(),
        "request_kind": request_kind,
        "provider": provider,
        "model": model,
        "system_prompt": _truncate((snapshot or {}).get("system"), limit),
        "request_content": _truncate(_serialize_request(snapshot), limit),
        "response_content": _truncate(getattr(resp, "text", None), limit),
        "tool_calls": _serialize_tool_calls(resp),
        "temperature": (snapshot or {}).get("temperature"),
        "max_tokens": (snapshot or {}).get("max_tokens"),
        "prompt_tokens": getattr(resp, "tokens_prompt", None),
        "completion_tokens": getattr(resp, "tokens_completion", None),
        "latency_ms": int(getattr(resp, "total_duration_ms", 0) or 0),
        "success": bool(getattr(resp, "success", False)),
        "error": _truncate(getattr(resp, "error", None), limit),
    }

    try:
        async with async_session() as db:
            await db.execute(
                text(
                    "INSERT INTO llm_traces ("
                    "  job_id, node_id, call_kind, request_kind, provider, model, "
                    "  system_prompt, request_content, response_content, tool_calls, "
                    "  temperature, max_tokens, prompt_tokens, completion_tokens, "
                    "  latency_ms, success, error"
                    ") VALUES ("
                    "  :job_id, :node_id, :call_kind, :request_kind, :provider, :model, "
                    "  :system_prompt, :request_content, :response_content, "
                    "  CAST(:tool_calls AS JSONB), "
                    "  :temperature, :max_tokens, :prompt_tokens, :completion_tokens, "
                    "  :latency_ms, :success, :error"
                    ")"
                ),
                params,
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "record_trace_failed: provider=%s model=%s kind=%s error=%s",
            provider, model, request_kind, exc,
        )
