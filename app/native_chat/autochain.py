"""The /confirm auto-chain — build a confirmed job end-to-end (§17.792, Phase 3b).

`/confirm <job>` drives the full pipeline in-process and streams it as chat text:
Phase 2 (research → ingest → compile, blocking) → DAG generation (blocking) →
execution (`/execute/all`, SSE) → the compiled deliverable. The two blocking
phases emit periodic status ticks so the OpenAI stream never goes silent for
minutes (the pipeline's `_post_with_keepalive` role). `/execute <job>` runs just
the execution stage for a job that already has a DAG.

Execution SSE events are translated to chat text: `node_token` deltas stream the
live generation, node lifecycle events become concise status lines, and the final
compiled output is fetched from `/exec/status` and appended as the deliverable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from app.native_chat import engine_client as ec, nl_commands

logger = logging.getLogger("scaffold.native_chat")

_TICK_INTERVAL = 15  # seconds between "still working" ticks during a blocking phase
_TERMINAL_EXEC_EVENTS = frozenset(
    {"pipeline_complete", "execution_failed", "blocked", "budget_exhausted"}
)


async def _blocking_with_ticks(path: str, payload: dict, tick_label: str) -> AsyncIterator[tuple]:
    """Run a blocking in-process POST, yielding ``("tick", elapsed)`` every
    ``_TICK_INTERVAL``s until it completes, then ``("result", (code, body))``."""
    async def _call():
        try:
            return await ec.request_json("POST", path, json=payload)
        except Exception as exc:  # transport error → surface as a soft failure
            return 0, str(exc)

    task = asyncio.create_task(_call())
    elapsed = 0
    while True:
        done, _ = await asyncio.wait({task}, timeout=_TICK_INTERVAL)
        if done:
            yield "result", task.result()
            return
        elapsed += _TICK_INTERVAL
        yield "tick", (tick_label, elapsed)


def _translate_exec_event(event: str, data: Any) -> str | None:
    """Map an /execute/all SSE event to chat text (or None to ignore)."""
    d = data if isinstance(data, dict) else {}
    label = d.get("title") or d.get("node_key") or ""
    if event == "node_token":
        return d.get("delta") or ""
    if event == "node_start":
        return f"\n▶ {label}"
    if event == "node_done":
        return f"\n✓ {label}"
    if event == "node_retry":
        return f"\n↻ retry {label}"
    if event == "node_failed":
        return f"\n✗ {label}: {d.get('error', 'failed')}"
    if event == "pipeline_complete":
        passed = d.get("passed")
        total = d.get("total_nodes") or d.get("total")
        return f"\n\n**Build complete** — {passed}/{total} steps passed."
    if event in ("blocked", "budget_exhausted"):
        return f"\n\n⚠️ {event.replace('_', ' ')}: {d.get('message') or d.get('status', '')}"
    if event == "execution_failed":
        return f"\n\n❌ Execution failed: {d.get('message', '')}"
    if event == "error":
        return f"\n\n❌ {d.get('detail') or d.get('message', 'error')}"
    return None  # dag_generated, queued, heartbeat, awaiting_* … ignored


async def _stream_execution(job_id: str) -> AsyncIterator[str]:
    """Relay /execute/all SSE as chat text, then append the compiled deliverable."""
    async for event, data in ec.stream_sse("/execute/all", json={"job_id": job_id}):
        piece = _translate_exec_event(event, data)
        if piece:
            yield piece
        if event in _TERMINAL_EXEC_EVENTS:
            break
    code, body = await ec.get_json(f"/exec/status/{job_id}")
    if code == 200 and isinstance(body, dict) and body.get("compiled_output"):
        yield "\n\n---\n\n" + str(body["compiled_output"]).strip()


async def run_confirm(job_ref: str) -> AsyncIterator[str]:
    """/confirm — Phase 2 → DAG → execute → deliverable, streamed."""
    job = await nl_commands._resolve_job(job_ref)
    if not job:
        yield f"I couldn't find a job matching \"{job_ref}\" to build."
        return
    job_id, title = job
    yield f"🚀 Building **{title or job_id[:8]}** (`{job_id[:8]}`)"

    # Phase 2 — research → ingest → compile (blocking, with ticks).
    yield "\n\n📚 Researching & compiling context…"
    code, body = 0, None
    async for kind, val in _blocking_with_ticks("/ideate/confirm", {"job_id": job_id}, "researching"):
        if kind == "tick":
            yield f"\n… {val[0]} ({val[1]}s)"
        else:
            code, body = val
    if code != 200:
        detail = body.get("detail") if isinstance(body, dict) else body
        yield f"\n❌ Phase 2 failed (HTTP {code}): {detail}"
        return
    yield "\n✓ Context compiled."

    # DAG generation (blocking, usually quick).
    yield "\n\n🧩 Planning the build…"
    code, body = 0, None
    async for kind, val in _blocking_with_ticks("/dag", {"job_id": job_id}, "planning"):
        if kind == "tick":
            yield f"\n… {val[0]} ({val[1]}s)"
        else:
            code, body = val
    if code != 200:
        detail = body.get("detail") if isinstance(body, dict) else body
        yield f"\n❌ Planning failed (HTTP {code}): {detail}"
        return
    steps = (body.get("task_count") if isinstance(body, dict) else None) or len(
        (body.get("tasks") or body.get("nodes") or []) if isinstance(body, dict) else []
    )
    yield f"\n✓ Planned {steps} steps. Executing…\n"

    # Execution (SSE) + compiled deliverable.
    async for piece in _stream_execution(job_id):
        yield piece


async def run_execute(job_ref: str) -> AsyncIterator[str]:
    """/execute — run the execution stage for a job that already has a DAG."""
    job = await nl_commands._resolve_job(job_ref)
    if not job:
        yield f"I couldn't find a job matching \"{job_ref}\" to execute."
        return
    job_id, title = job
    yield f"▶️ Executing **{title or job_id[:8]}** (`{job_id[:8]}`)\n"
    async for piece in _stream_execution(job_id):
        yield piece
