"""
execution_agent.py  --  Step 15

Executes DAG nodes one at a time with user confirmation gate.

Flow per node:
  fetch next pending node (deps satisfied)
    -> optimize prompt (Step 14)
      -> execute via model_router
        -> verify output (qwen2.5:7b)
          -> persist result + update status
            -> return to user for approval

Error recovery cascade (per spec):
  1. Retry same model 3x (handled by model_router)
  2. Swap to local fallback
  3. Replan node (simplified: mark failed + surface to user)
  4. Log + present to user
"""

import asyncio
import json
import logging
import re
import time
from typing import AsyncGenerator, Literal
from uuid import UUID

try:
    from json_repair import repair_json
except ImportError:  # pragma: no cover
    repair_json = None

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session
from app import model_router
from app.config import settings, get_model
from app.modules.execution_compile import _compile_output  # re-exported for test patches
from app.modules.execution_verify import (
    VERIFY_SYSTEM, _verify_output,  # re-exported for test patches
    _verify_codegen_output,  # §17.429 — stricter CodeGen verifier
    extract_brief_goal,
    collect_upstream_code,
    _is_validation_llm_node,
    check_validation_citations,
    check_validation_citation_coverage,
)
from app.modules.execution_codegen_gate import (
    check_python_syntax,
    format_syntax_reason,
)
from app.sandbox.codegen_check import codegen_exec_smoke  # §17.434
from app.modules.prompt_optimizer import optimize_prompt
# §17.389 — re-export the canonical prompt strings from prompt_assembly.
# Pre-§17.389 these three constants were duplicated literally here AND
# in prompt_assembly.py (~31.8 KB of byte-equal mirrors that drifted
# silently until §17.384 added a parity test). §17.389 makes
# prompt_assembly the single source of truth and re-exports here so
# every `from app.modules.execution_agent import EXECUTION_SYSTEM_*`
# call site keeps working unchanged.
from app.modules.prompt_assembly import (  # noqa: F401  re-exported for callers
    EXECUTION_SYSTEM_LLM,
    EXECUTION_SYSTEM_CODEGEN,
    EXECUTION_SYSTEM_RUNBOOK,
)
from app.modules.rag_pipeline import query_rag
from app.utils.cost_tracking import current_job_id, current_node_id
from app.utils.llm_retry import chat_until_nonempty  # §17.465

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sprint X.24 — process-wide cap on concurrent execute_all_nodes runs.
# Lazy-init so the value is read from settings at first use; tests reset
# via _reset_execution_slot_sem() after mutating settings. Single-process
# uvicorn (worker=1) means a plain module global is sufficient — no
# cross-process Redis lock needed. If we ever go multi-worker the cap
# must move to Redis.
# ---------------------------------------------------------------------------
_execution_slot_sem: asyncio.Semaphore | None = None

def _get_execution_slot_sem() -> asyncio.Semaphore:
    global _execution_slot_sem
    if _execution_slot_sem is None:
        _execution_slot_sem = asyncio.Semaphore(
            settings.execution_global_concurrency
        )
    return _execution_slot_sem

def _reset_execution_slot_sem() -> None:
    """Test hook — drop the cached semaphore so the next call re-reads settings."""
    global _execution_slot_sem
    _execution_slot_sem = None

def executor_inflight_count() -> int:
    """§17.277 — public read of in-flight ``execute_all_nodes`` concurrency
    slots. Telemetry-only; returns ``0`` when the semaphore hasn't been
    initialized yet (lifespan pre-warmup or fresh test fixture).

    Encapsulates the access to ``asyncio.Semaphore._value``: the stdlib
    exposes no public inflight count, so the private read has to live
    SOMEWHERE. Putting it in the module that owns the semaphore (here,
    not in ``app/observability/metrics.py``) means future changes to
    the slot mechanism stay co-located with the readers, and external
    callers (``metrics.py``, future health endpoints) consume a
    sanctioned signature instead of reaching into another module's
    private state.
    """
    if _execution_slot_sem is None:
        return 0
    cap = settings.execution_global_concurrency
    return max(0, cap - _execution_slot_sem._value)

# ---------------------------------------------------------------------------
# Sprint X.24 — detached cleanup tasks for cancelled execute_all_nodes runs.
# Live verification surfaced that ``await`` calls inside execute_all_nodes'
# ``finally`` block were being interrupted by re-entrant cancellation
# (Starlette/uvicorn cancels the request task when the client disconnects;
# the first CancelledError is caught by the except handler, but the next
# await inside ``finally`` raises again, abandoning the DB cleanup half
# done). Spawning cleanup as a detached task — same pattern W.10 uses for
# the assist verifier — decouples it from the cancelled chain so it runs
# to completion regardless. Strong refs in _CLEANUP_TASKS keep tasks alive
# (asyncio.create_task only holds a weak ref).
# ---------------------------------------------------------------------------
_CLEANUP_TASKS: set[asyncio.Task] = set()

async def _cleanup_stuck_running_job(job_id: str, exit_reason: str | None) -> None:
    """Reset a job stuck at ``running`` to a terminal status and mark any
    orphaned ``running`` dag_nodes as ``failed``. No-op if the job is no
    longer at ``running`` (clean exits already set ``completed``/``blocked``)."""
    try:
        async with async_session() as db:
            status_row = await db.execute(
                text("SELECT status FROM jobs WHERE id = :jid"),
                {"jid": job_id},
            )
            current_status = status_row.scalar()
            if current_status != "running":
                return
            if exit_reason == "cancelled":
                terminal = "cancelled"
            elif exit_reason == "exception":
                terminal = "failed"
            else:
                terminal = "failed"  # safety default (Session 3 early-return etc.)
            await db.execute(
                text("UPDATE jobs SET status = :s, updated_at = now() WHERE id = :jid"),
                {"s": terminal, "jid": job_id},
            )
            await db.execute(
                text(
                    "UPDATE dag_nodes SET status = 'failed', completed_at = NOW() "
                    "WHERE job_id = :jid AND status = 'running'"
                ),
                {"jid": job_id},
            )
            await db.commit()
            logger.warning(
                "execute_all_nodes_cleanup: job=%s running->%s (reason=%s)",
                job_id, terminal, exit_reason,
            )
    except Exception as exc:
        logger.error(
            "execute_all_nodes_cleanup_failed: job=%s error=%s", job_id, exc,
        )

def _spawn_cleanup_task(job_id: str, exit_reason: str | None) -> asyncio.Task:
    """Schedule cleanup as a detached task with a strong ref to prevent GC."""
    task = asyncio.create_task(_cleanup_stuck_running_job(job_id, exit_reason))
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)
    return task

async def drain_cleanup_tasks(timeout: float = 5.0) -> None:
    """Test hook — wait for all in-flight cleanup tasks to complete."""
    if _CLEANUP_TASKS:
        await asyncio.wait_for(
            asyncio.gather(*list(_CLEANUP_TASKS), return_exceptions=True),
            timeout=timeout,
        )

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_job(db: AsyncSession, job_id: str) -> dict | None:
    row = await db.execute(
        text("SELECT id, status, refined_brief FROM jobs WHERE id = :id"),
        {"id": job_id},
    )
    r = row.mappings().first()
    return dict(r) if r else None

async def _orphan_diagnostic(db: AsyncSession, job_id: str) -> dict:
    """Build the diagnostic payload for an "already executing" 409.

    Returns a dict that callers (the concurrent-execution guard) embed
    into the SSE error event so the operator can decide between
    "wait for the reaper" and "call ``POST /jobs/cleanup`` now."

    Fields:
      - ``node_orphan_threshold_minutes``: how long a ``running`` node
        can sit before Stage-0 of ``reap_stale_jobs`` resets it.
      - ``cleanup_interval_seconds``: how often the reaper loop fires.
      - ``running_nodes``: list of ``{node_key, started_at,
        seconds_until_reap}`` for every ``status='running'`` dag_node
        belonging to this job, sorted by ``started_at`` ASC.
        ``seconds_until_reap`` is negative if the node is already past
        its threshold (next reaper cycle will reset it).
      - ``oldest_started_at``: ISO timestamp of the longest-running
        node, or ``None`` if no running nodes exist.
      - ``suggested_action``: ``"wait_for_reaper"`` if any node is past
        due, ``"call_cleanup_or_wait"`` if any node is approaching
        threshold (within ``cleanup_interval_seconds``), else
        ``"wait_or_inspect"`` (a legit run in progress).
      - ``cleanup_endpoint``: ``"POST /jobs/cleanup"`` — the force-reap
        path that bypasses the loop interval.

    Fail-soft: a DB error here must not mask the 409 itself, so
    callers wrap this in try/except and fall back to a minimal payload.
    """
    threshold_min = settings.node_orphan_threshold_minutes
    interval_s = settings.cleanup_interval_seconds

    rows = await db.execute(
        text(
            "SELECT node_key, started_at, "
            "       EXTRACT(EPOCH FROM (started_at + make_interval(mins => :thresh) - NOW())) "
            "         AS seconds_until_reap "
            "  FROM dag_nodes "
            " WHERE job_id = :jid AND status = 'running' "
            " ORDER BY started_at ASC NULLS FIRST"
        ),
        {"jid": job_id, "thresh": threshold_min},
    )
    running = []
    oldest_started_at = None
    any_past_due = False
    any_near_due = False
    for r in rows.mappings():
        sec = r["seconds_until_reap"]
        sec_int = int(sec) if sec is not None else None
        running.append({
            "node_key": r["node_key"],
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "seconds_until_reap": sec_int,
        })
        if oldest_started_at is None and r["started_at"]:
            oldest_started_at = r["started_at"].isoformat()
        if sec_int is not None:
            if sec_int <= 0:
                any_past_due = True
            elif sec_int <= interval_s:
                any_near_due = True

    if any_past_due:
        suggested = "wait_for_reaper"
    elif any_near_due:
        suggested = "call_cleanup_or_wait"
    else:
        suggested = "wait_or_inspect"

    return {
        "node_orphan_threshold_minutes": threshold_min,
        "cleanup_interval_seconds": interval_s,
        "running_nodes": running,
        "oldest_started_at": oldest_started_at,
        "suggested_action": suggested,
        "cleanup_endpoint": "POST /jobs/cleanup",
    }

async def _get_next_node(db: AsyncSession, job_id: str) -> dict | None:
    """Atomically claim the next dep-satisfied pending node.

    Uses compound UPDATE ... WHERE id = (...) AND status='pending' RETURNING *
    so only one concurrent executor can claim a given node.

    Concurrency precondition (§17.409, arch-review R5): the dependency check
    and the claim are two statements, not one. This is safe under the current
    **one-executor-per-job** model — `execute_next_node` claims a single node
    per call and the job's loop is sequential, so no peer can revert a
    dependency from 'done' to 'pending' between the check and the claim. If
    same-job parallel execution is ever introduced, fold the dep-satisfied
    predicate into the claim's WHERE (a NOT EXISTS over unfinished deps) so the
    claim is atomic w.r.t. dependency state.
    """
    rows = await db.execute(
        text("""
            SELECT id, node_key, depends_on, execution_order
            FROM dag_nodes
            WHERE job_id = :job_id AND status = 'pending'
            ORDER BY execution_order ASC
        """),
        {"job_id": job_id},
    )
    candidates = [dict(r) for r in rows.mappings()]
    if not candidates:
        return None

    done_rows = await db.execute(
        text("SELECT node_key FROM dag_nodes WHERE job_id = :job_id AND status IN ('done', 'skipped')"),
        {"job_id": job_id},
    )
    done_keys = {r[0] for r in done_rows}

    target = next(
        (c for c in candidates if all(d in done_keys for d in (c.get("depends_on") or []))),
        None,
    )
    if target is None:
        return None

    claim = await db.execute(
        text("""
            UPDATE dag_nodes
            SET status = 'running', started_at = NOW()
            WHERE id = :id AND status = 'pending'
            RETURNING id, node_key, title, node_type, depends_on,
                      assigned_model, prompt_template, execution_order, tool, domain,
                      retry_count, last_verification_reason
        """),
        {"id": str(target["id"])},
    )
    claimed = claim.mappings().first()
    await db.commit()
    return dict(claimed) if claimed else None

async def _set_node_status(
    db: AsyncSession,
    node_id: str,
    status: str,
    output: str | None = None,
    optimized_prompt: str | None = None,
    verification_reason: str | None = None,
    confidence: float | None = None,
    set_confidence: bool = False,
) -> None:
    """Update node status. COALESCE preserves prior values when caller passes None.

    ``verification_reason`` writes to ``last_verification_reason``. When the
    caller passes ``None`` we COALESCE so a successful pass after a prior
    failure preserves the historical reason — useful for audit; not
    re-injected because the read path on the next retry is gated by status
    + retry_count, not by the column itself.

    ``confidence`` is written **only** when ``set_confidence=True`` (§17.407 —
    folded in from a separate UPDATE+commit so the verifier's confidence lands
    atomically with the terminal status, one round-trip instead of two). The
    CASE guard lets the verification path write ``None`` explicitly (skipped /
    zero-confidence nodes) while every other caller leaves the column untouched.

    Migration 026 added ``last_verification_reason``; pre-026 deployments of
    this code path raise ``UndefinedColumn`` on the first call. Run migrations
    on startup (default) or via `make migrate` after deploy.
    """
    await db.execute(
        text("""
            UPDATE dag_nodes
            SET status = :status,
                output_text = COALESCE(:output, output_text),
                optimized_prompt = COALESCE(:optimized_prompt, optimized_prompt),
                last_verification_reason = COALESCE(:verification_reason, last_verification_reason),
                confidence = CASE WHEN :set_confidence
                             THEN CAST(:confidence AS double precision)
                             ELSE confidence END,
                completed_at = CASE WHEN :status IN ('done','failed','skipped')
                               THEN NOW() ELSE completed_at END
            WHERE id = :id
        """),
        {
            "id": str(node_id),
            "status": status,
            "output": output,
            "optimized_prompt": optimized_prompt,
            "verification_reason": verification_reason,
            "confidence": confidence,
            "set_confidence": set_confidence,
        },
    )
    await db.commit()

async def _log_execution(
    db: AsyncSession,
    job_id: str,
    node_id: str,
    level: str,
    message: str,
    details: dict | None = None,
) -> None:
    await db.execute(
        text("""
            INSERT INTO execution_logs (job_id, node_id, log_level, message, details)
            VALUES (:job_id, :node_id, :level, :message, :details)
        """),
        {
            "job_id": job_id,
            "node_id": str(node_id),
            "level": level,
            "message": message,
            "details": json.dumps(details or {}),
        },
    )
    await db.commit()

async def _all_nodes_done(db: AsyncSession, job_id: str) -> bool:
    row = await db.execute(
        text("""
            SELECT COUNT(*) FROM dag_nodes
            WHERE job_id = :job_id AND status NOT IN ('done', 'skipped')
        """),
        {"job_id": job_id},
    )
    return row.scalar() == 0

# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------

def _truncate_output(content: str, max_chars: int) -> str:
    """Truncate content preserving first/last 20%, with a marker in the middle."""
    if len(content) <= max_chars:
        return content
    keep = max_chars
    head_len = int(keep * 0.2)
    tail_len = int(keep * 0.2)
    removed = len(content) - head_len - tail_len
    return (
        content[:head_len]
        + f"\n[...truncated {removed} chars...]\n"
        + content[-tail_len:]
    )

async def _fetch_upstream_outputs(
    db, job_id: str, depends_on: list[str]
) -> dict[str, tuple[str, float | None]]:
    """Fetch (output_text, confidence) for upstream nodes by node_key.

    §17.477 — confidence is the verifier's 0..1 score (NULL for un-verified
    or skipped-verify nodes). The caller annotates each upstream section with
    it and, when over the size cap, weights the truncation budget by it.
    """
    if not depends_on:
        return {}
    rows = await db.execute(
        text(
            "SELECT node_key, output_text, confidence FROM dag_nodes "
            "WHERE job_id = :jid AND node_key = ANY(:keys) AND status = 'done'"
        ),
        {"jid": job_id, "keys": depends_on},
    )
    return {
        r.node_key: (r.output_text or "", r.confidence)
        for r in rows.fetchall()
    }


def _format_upstream_block(upstream_outputs: dict, node_key: str = "") -> str:
    """§17.477 — render the MANDATORY-CONTEXT upstream block from
    ``{node_key: (text, confidence)}`` items.

    - Annotates each section header with the upstream node's verifier
      confidence (omitted for NULL / un-verified nodes).
    - When the total exceeds ``settings.max_upstream_chars``, allocates each
      node's surviving char budget by ``confidence × length`` (NULL = 0.5,
      neutral) when ``settings.upstream_confidence_ranking_enabled``, else by
      length alone (legacy). Keeps the ``compile_output_min_chunk`` floor.

    Returns ``""`` for empty input. The result is a PREFIX ending in the YOUR
    TASK header — prepend it to the raw prompt. Accepts a bare ``str`` value
    defensively (degrades to no confidence) so mocks need not return tuples.
    """
    if not upstream_outputs:
        return ""
    texts: dict[str, str] = {}
    confs: dict[str, float | None] = {}
    for nk, val in upstream_outputs.items():
        if isinstance(val, tuple):
            txt, conf = val
        else:
            txt, conf = val, None
        texts[nk] = txt
        confs[nk] = conf

    total_chars = sum(len(v) for v in texts.values())
    truncated_keys: list[str] = []
    if total_chars > settings.max_upstream_chars:
        if settings.upstream_confidence_ranking_enabled:
            weights = {
                nk: (confs[nk] if confs[nk] is not None else 0.5) * len(texts[nk])
                for nk in texts
            }
            denom = sum(weights.values()) or 1.0
        else:
            weights = {nk: float(len(texts[nk])) for nk in texts}
            denom = float(total_chars) or 1.0
        for nk in texts:
            orig_len = len(texts[nk])
            share = max(
                settings.compile_output_min_chunk,
                int(settings.max_upstream_chars * weights[nk] / denom),
            )
            if orig_len > share:
                texts[nk] = _truncate_output(texts[nk], share)
                truncated_keys.append(nk)
        logger.info(
            "upstream_truncated",
            extra=dict(
                event="upstream_truncated",
                node_key=node_key,
                original_chars=total_chars,
                truncated_chars=sum(len(v) for v in texts.values()),
                upstream_nodes=truncated_keys,
                confidence_weighted=settings.upstream_confidence_ranking_enabled,
            ),
        )

    parts = []
    for nk in texts:
        c = confs[nk]
        suffix = f" (confidence: {c:.2f})" if c is not None else ""
        parts.append(f"### {nk}{suffix}\n{texts[nk]}")
    return (
        "## Upstream Node Outputs (MANDATORY CONTEXT — your output MUST build on and be consistent with this work)\n"
        + "\n\n".join(parts)
        + "\n\n---\n\n## YOUR TASK (build on the upstream outputs above — do NOT rewrite or contradict them):\n"
    )


async def _fetch_rag_context(query: str, top_k: int = 2, domain: str | None = None) -> str:
    """Query RAG pipeline and format results as grounding context."""
    try:
        rag = await query_rag(query, top_k=top_k, skip_rerank=False, domain=domain)
        if rag.get("status") != "ok" or not rag.get("results"):
            return ""
        entries = []
        for r in rag["results"]:
            vec_score = r.get("scores", {}).get("vector", 0.0)
            rrf_score = r.get("scores", {}).get("rrf", 0.0)
            if vec_score < settings.rag_cosine_floor:
                logger.info("RAG skip low-relevance doc: %s (cosine=%.3f, rrf=%.4f, floor=%.3f)", r.get("title", "?"), vec_score, rrf_score, settings.rag_cosine_floor)
                continue
            entries.append(f"[{r['title']}] {r['content']}")
        if not entries:
            logger.info("RAG: all results below cosine relevance threshold (%.3f)", settings.rag_cosine_floor)
        return "\n\n".join(entries)
    except Exception as e:
        logger.warning("RAG grounding failed: %s", e)
        return ""

def _system_for_tool(tool: str) -> str:
    """Return the appropriate system prompt for a node tool type.

    Case-insensitive: VALID_TOOLS uses canonical casing ("CodeGen", "Shell")
    but a hand-edited row carrying ``"codegen"`` / ``"shell"`` should still
    get the right system prompt.

    §17.359 — ``Shell`` tool routes here too. When ``shell_tool_enabled`` is
    False (default), Shell nodes get the runbook prompt and dispatch via the
    LLM executor — text-only output framed as "Run this:" for the human to
    perform. When the flag is True, ``execute_next_node`` short-circuits to
    a real shell backend (NotImplementedError until wired) before this
    prompt is selected.
    """
    t = tool.lower()
    if t == "codegen":
        return EXECUTION_SYSTEM_CODEGEN
    if t == "shell":
        return EXECUTION_SYSTEM_RUNBOOK
    return EXECUTION_SYSTEM_LLM

def _build_prompt(node: dict, brief: dict) -> str:
    """Build execution prompt from node template + brief context.

    Sprint W.1 — when ``retry_count > 0`` AND ``last_verification_reason``
    is non-empty, a "Reviewer feedback" block is prepended so the LLM sees
    the prior rejection before re-attempting. Without this loop a retry
    sees the identical prompt and produces the identical rejected output.
    """
    template = node.get("prompt_template") or ""
    title = node["title"]
    goal = brief.get("description", "") if brief else ""
    if not goal and brief:
        goals = brief.get("goals", [])
        goal = goals[0] if goals else ""

    if template:
        body = f"{template}\n\nContext: {goal}"
    else:
        body = (
            f"Execute this task: {title}\n\n"
            f"Project goal: {goal}\n\n"
            f"Produce a complete, actionable output for this task. "
            f"Base your response on the ground truth provided above where relevant."
        )

    feedback = _format_reviewer_feedback(node)
    return f"{feedback}{body}" if feedback else body

# §17.299 — `_format_reviewer_feedback` lives in `execution_retry` so the
# audit's hot-path-file convention is met. Re-import keeps the original
# name reachable from this module (tests + the `_build_prompt` caller above
# both reference it on `execution_agent`).
from app.modules.execution_retry import _format_reviewer_feedback  # noqa: E402

# ---------------------------------------------------------------------------
# Public API

# ── Tool Dispatch ────────────────────────────────────────────

async def _searxng_search(query: str, max_results: int = 5) -> str:
    """Call SearXNG JSON API, return formatted results."""
    try:
        from app.utils.http_clients import get_searxng_client
        client = get_searxng_client()
        resp = await client.get(
            "/search",
            params={"q": query, "format": "json", "categories": "general"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])[:max_results]
        if not results:
            return "No search results found."
        lines = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "Untitled")
            snippet = r.get("content", "No snippet")
            url = r.get("url", "")
            lines.append(f"[{i}] {title}\n    {snippet}\n    {url}")
        return "\n\n".join(lines)
    except Exception as e:
        logger.warning("searxng_search_failed: %s", e)
        return f"SearXNG search failed: {e}"

async def _milvus_search(query: str, node_key: str = "?", domain: str | None = None) -> str:
    """Call query_rag(), return formatted context with structured logging."""
    try:
        rag_result = await query_rag(query, domain=domain, top_k=5)
        results = rag_result.get("results", [])
        metadata = rag_result.get("metadata", {})

        # Structured retrieval log
        domains_found = set(r.get("domain", "unknown") for r in results)
        formatted_lines = []
        for i, doc in enumerate(results, 1):
            topic = doc.get("title", "Unknown")
            content = doc.get("content", "")[:500]
            formatted_lines.append(f"[{i}] {topic}\n    {content}")
        formatted = "\n\n".join(formatted_lines) if formatted_lines else ""
        total_chars = len(formatted)

        logger.info(
            "milvus_retrieval",
            extra=dict(
                event="milvus_retrieval",
                node_key=node_key,
                domain=",".join(sorted(domains_found)) if domains_found else "all",
                top_k=5,
                results_returned=len(results),
                total_chars_injected=total_chars,
                reranker_used=metadata.get("reranked", False),
            ),
        )

        # Structured rerank log (if reranking was used)
        if metadata.get("reranked", False):
            top_score = 0.0
            if results:
                scores = [r.get("scores", {}).get("rerank", 0.0) for r in results]
                top_score = max(scores) if scores else 0.0
            logger.info(
                "milvus_rerank",
                extra=dict(
                    event="milvus_rerank",
                    node_key=node_key,
                    candidates_in=metadata.get("fused_count", 0),
                    candidates_out=len(results),
                    top_score=round(top_score, 4),
                ),
            )

        if not results:
            return "No knowledge base results found."
        return formatted
    except Exception as e:
        logger.warning(
            "milvus_search_failed",
            extra=dict(event="milvus_search_failed", node_key=node_key, error=str(e)),
        )
        return f"Knowledge base search failed: {e}"

# ---------------------------------------------------------------------------

async def execute_next_node(
    job_id: str,
    skip_optimize: bool = False,
    skip_verify: bool = False,
    model_overrides: dict | None = None,
) -> dict:
    """Execute the next pending node in the DAG.

    Session-lifetime policy: this function manages its own short-lived
    async sessions around each DB phase. Long LLM work (optimize/execute/
    verify) runs with NO session open — connections are not held across
    10+ minute model calls.
    """
    # ---- Phase 1 (fast session): validate + claim next node ----
    async with async_session() as db:
        job = await _get_job(db, job_id)
        if not job:
            return {"status": "error", "message": f"Job {job_id} not found"}
        if job["status"] not in ("running", "executing", "planning"):
            return {"status": "error", "message": f"Job status is '{job['status']}' — not executable"}

        node = await _get_next_node(db, job_id)
        if node is None:
            if await _all_nodes_done(db, job_id):
                result = await db.execute(
                    text(
                        "UPDATE jobs SET status = 'completed' "
                        "WHERE id = :jid AND status != 'completed' "
                        "RETURNING id"
                    ),
                    {"jid": job_id},
                )
                flipped = result.fetchone()
                await db.commit()
                if flipped is not None:
                    # X.2: _compile_output now returns (text, was_synthesized).
                    compiled, was_synthesized = await _compile_output(job_id, db)
                    if compiled:
                        await db.execute(
                            text(
                                "UPDATE jobs SET compiled_output = :co, "
                                "compiled_output_synthesized = :syn WHERE id = :jid"
                            ),
                            {"co": compiled, "syn": was_synthesized, "jid": job_id},
                        )
                        await db.commit()
                return {"status": "complete", "message": "All nodes done. Job complete."}

            # Partial compile for blocked jobs (#22, cached)
            partial_result = None
            try:
                cached_row = await db.execute(
                    text("SELECT compiled_output FROM jobs WHERE id = :jid"),
                    {"jid": job_id},
                )
                cached_output = cached_row.scalar()
                if cached_output:
                    partial_result = cached_output
                    await db.execute(
                        text("UPDATE jobs SET status = 'blocked' WHERE id = :jid AND status != 'blocked'"),
                        {"jid": job_id},
                    )
                    await db.commit()
                    logger.info("partial_compiled_cache_hit: job=%s chars=%s", job_id, len(partial_result))
                else:
                    # X.2: tuple return; persist synthesized flag too.
                    partial_result, partial_synthesized = await _compile_output(job_id, db)
                    if partial_result:
                        await db.execute(
                            text(
                                "UPDATE jobs SET compiled_output = :co, "
                                "compiled_output_synthesized = :syn, "
                                "status = 'blocked' WHERE id = :jid"
                            ),
                            {"co": partial_result, "syn": partial_synthesized, "jid": job_id},
                        )
                    else:
                        await db.execute(
                            text("UPDATE jobs SET status = 'blocked' WHERE id = :jid"),
                            {"jid": job_id},
                        )
                    await db.commit()
                    logger.info("partial_compiled: job=%s chars=%s", job_id, len(partial_result) if partial_result else 0)
            except Exception as exc:
                logger.warning("partial_compile_failed: job=%s error=%s", job_id, str(exc))

            # §17.295 — distinguish "blocked by failed upstream" (operator
            # needs `/exec retry`) from "blocked by pending/running upstream"
            # (operator just waits). Pre-§17.295 the response merged both
            # under a single ambiguous label, AND the query only included
            # pending-blocked-by-failed nodes — pending-blocked-by-pending
            # nodes were silently dropped, hiding the actual wait state
            # from operators.
            blocked_nodes = []
            actionable_count = 0
            waiting_count = 0
            try:
                _all = await db.execute(
                    text("SELECT node_key, title, status, depends_on FROM dag_nodes WHERE job_id = :jid"),
                    {"jid": job_id},
                )
                _rows = _all.fetchall()
                status_by_key = {r.node_key: r.status for r in _rows}
                # Statuses that mean "dep is unfinished — caller is blocked".
                # `done` / `skipped` are success-terminal; anything else here
                # is a live blocker. The cause precedence below classifies
                # the dominant kind.
                _non_terminal = {"failed", "blocked", "pending", "running"}
                for r in _rows:
                    if r.status != "pending":
                        continue
                    deps = r.depends_on if isinstance(r.depends_on, list) else []
                    blocked_by_objs = [
                        {"node_key": k, "status": status_by_key.get(k, "unknown")}
                        for k in deps
                        if status_by_key.get(k, "done") in _non_terminal
                    ]
                    if not blocked_by_objs:
                        continue
                    # Cause precedence: any failed/blocked dep → "failed"
                    # (operator action: retry / skip). Otherwise deps are
                    # pending or running → "waiting" (operator: wait).
                    dep_statuses = {b["status"] for b in blocked_by_objs}
                    if dep_statuses & {"failed", "blocked"}:
                        cause = "failed"
                        actionable_count += 1
                    else:
                        cause = "waiting"
                        waiting_count += 1
                    blocked_nodes.append({
                        "node_key": r.node_key,
                        "title": r.title,
                        "blocked_by": blocked_by_objs,
                        "cause": cause,
                    })
            except Exception as exc:
                logger.warning("blocked_node_query_failed: job=%s error=%s", job_id, str(exc))

            # §17.295 — cause-aware top-level message so operators see the
            # split without needing to walk the per-node list.
            if actionable_count and waiting_count:
                message = (
                    f"No executable nodes — {actionable_count} need action "
                    f"(failed upstream), {waiting_count} waiting on upstream."
                )
            elif actionable_count:
                message = (
                    f"No executable nodes — {actionable_count} blocked by "
                    f"failed upstream. Use `/exec retry <job_id> <node_key>`."
                )
            elif waiting_count:
                message = (
                    f"No executable nodes — {waiting_count} waiting on "
                    f"upstream to finish."
                )
            else:
                # No pending nodes had unfinished deps but _get_next_node
                # still returned None. Rare (e.g. all nodes already
                # terminal but autocomplete didn't fire yet). Keep the
                # pre-§17.295 generic message for this edge case.
                message = "No executable nodes — dependencies not satisfied"

            return {
                "status": "blocked",
                "message": message,
                "blocked_nodes": blocked_nodes,
                "actionable_count": actionable_count,
                "waiting_count": waiting_count,
            }

        # Node claimed. Snapshot fields we need after session closes.
        node_id = node["id"]
        title = node["title"]
        node_key = node["node_key"]
        # §17.376 — node_type captured so the post-verify citation guard
        # can detect type=validation LLM nodes without a re-fetch.
        node_type_value = node.get("node_type")
        _raw_model = node.get("assigned_model", "")
        _assigned = _raw_model if _raw_model and str(_raw_model).lower() not in ("none", "null") else ""
        # Tool comparisons are case-insensitive — VALID_TOOLS pins the
        # canonical capitalization, but defensive matching here keeps the
        # node's downstream behavior consistent if a DAG generator emits a
        # lowercase variant or a hand-edited row carries one.
        tool = (node.get("tool") or "LLM").strip()
        tool_lower = tool.lower()

        # §17.89 Pattern 3 — fold the per-node `_assigned` model AND the
        # codegen-override into the per-call overrides dict so the dispatch
        # below can route via role= (and pick up the configured provider)
        # while still honoring the user's per-node model choice.
        # CodeGen override — only when assigned_model is blank.
        exec_overrides = dict(model_overrides or {})
        if tool_lower == "codegen" and not _assigned:
            exec_role = "model_coder"
        else:
            exec_role = "model_general"
            if _assigned:
                exec_overrides["model_general"] = _assigned
        exec_model = get_model(exec_role, exec_overrides)
        verifier_model = get_model("model_verifier", model_overrides)

        # ── Shell: §17.359 seam ──
        # When ``shell_tool_enabled`` is True we expect a real shell backend
        # bolted on here (subprocess dispatch, sandboxed exec, etc.). Until
        # that lands, the flag-on path must fail loudly rather than silently
        # downgrade to the runbook prompt — otherwise an operator flips the
        # flag, sees text output, and assumes the host was modified. The
        # flag-off path falls through to the normal LLM dispatch below, where
        # ``_system_for_tool("Shell")`` returns ``EXECUTION_SYSTEM_RUNBOOK``.
        if tool_lower == "shell" and settings.shell_tool_enabled:
            raise NotImplementedError(
                "Shell tool execution requested but no backend wired. "
                "Either disable settings.shell_tool_enabled, or implement "
                "a shell executor here (subprocess/sandboxed) and route "
                "Shell-tagged nodes to it. See §17.359."
            )

        # ── Human: single atomic UPDATE short-circuit (H3) ──
        if tool.lower() in ("human", "human_review"):
            skip_msg = "Skipped: human review not required in auto mode"
            await db.execute(
                text(
                    "UPDATE dag_nodes "
                    "SET status = 'done', output_text = :o, completed_at = NOW() "
                    "WHERE id = :nid"
                ),
                {"o": skip_msg, "nid": str(node_id)},
            )
            await db.commit()
            await _log_execution(db, job_id, str(node_id), "info", f"Tool dispatch: {tool} skipped")
            logger.info("tool_dispatch: %s skip node=%s", tool, node_key)
            return {
                "status": "done",
                "node_key": node_key,
                "title": title,
                "output": skip_msg,
                "passed": True,
                "reason": "Tool dispatch: node skipped",
                "confidence": 1.0,
                "model_used": "none (skipped)",
                "tool": tool,
            }

        logger.info("node_execution_started: node='%s' job=%s model=%s", title, job_id, exec_model)

        brief = job.get("refined_brief") or {}
        depends_on = node.get("depends_on") or []
        # Sprint X.4 — upstream-fetch try/except wrap. Same rationale as W.4:
        # without this, a DB-layer failure inside _fetch_upstream_outputs
        # (connection drop, asyncpg interface error, etc.) bubbles up to
        # execute_all_nodes' generic exception handler, which forces the
        # node 'failed' but leaves last_verification_reason NULL — defeating
        # W.1's retry-feedback loop on the next /exec/retry. A fresh session
        # is used for the error-persist path because the outer Phase 1
        # session may be poisoned by the failure.
        try:
            upstream_outputs = (
                await _fetch_upstream_outputs(db, job_id, depends_on)
                if depends_on else {}
            )
        except Exception as fetch_exc:
            err_msg = f"upstream fetch error: {fetch_exc}"
            logger.exception(
                "node_upstream_fetch_failed: node='%s' job=%s error=%s",
                title, job_id, fetch_exc,
            )
            async with async_session() as _err_db:
                await _set_node_status(
                    _err_db, node_id, "failed",
                    verification_reason=err_msg,
                )
                await _log_execution(_err_db, job_id, node_id, "error", err_msg)
            return {
                "status": "failed",
                "node_key": node_key,
                "title": title,
                "error": err_msg,
                "verification_reason": err_msg,
                "reason": "upstream_fetch_error",
                "message": (
                    "Upstream output fetch failed. Check parent node states; "
                    "retry once the DAG is healthy."
                ),
            }

        node_snapshot = {
            "node_key": node_key,
            "title": title,
            "prompt_template": node.get("prompt_template"),
            "domain": node.get("domain"),
            # Sprint W.1 — _build_prompt prepends a Reviewer feedback block
            # when retry_count > 0 AND a prior rejection reason is on the row.
            "retry_count": node.get("retry_count") or 0,
            "last_verification_reason": node.get("last_verification_reason"),
        }
    # ---- Session closed. LLM phase begins. ----

    # Sprint J.3.a — set cost-tracking ContextVars so every model_router
    # call inside this task records its log row tagged with this job +
    # node. The values persist for the rest of this asyncio task; since
    # execute_next_node runs as its own create_task() under
    # execute_all_nodes (and ContextVars are per-task), no manual reset
    # is needed — the task ends when the function returns.
    current_job_id.set(job_id)
    current_node_id.set(str(node_id))

    # Sprint W.4 — prompt-build try/except wrap. The whole assembly path
    # (build → RAG/SearXNG/Milvus injection → upstream stitching → optimize)
    # is wrapped so a failure here marks the node 'failed' with
    # last_verification_reason populated, consistent with the timeout +
    # exec-error paths below. Without this wrap, an exception here bubbles
    # up to execute_all_nodes' generic handler, which forces the node to
    # 'failed' but leaves last_verification_reason NULL — defeating W.1's
    # retry-feedback loop on the subsequent /exec/retry.
    try:
        # Build raw prompt.
        raw_prompt = _build_prompt(node_snapshot, brief)

        # Inject RAG grounding BEFORE optimize (optimizer should see grounded content).
        project_goal = " ".join(brief.get("goals", [])) if brief else ""
        rag_query = f"{project_goal}: {title}" if project_goal else title
        job_domain = brief.get("domain") if brief else None
        # §17.517 — general grounding fans out across all domains by default so
        # research ingested under a different (heuristic) partition than the
        # job's domain is still found. The cosine floor + reranker filter noise.
        grounding_domain = None if settings.execution_grounding_cross_domain else job_domain

        if tool_lower == "milvus":
            rag_block = await _milvus_search(title, node_key=node_key, domain=node_snapshot.get("domain"))
            if rag_block:
                raw_prompt = f"{raw_prompt}\n\n## Knowledge Base Results\n{rag_block}"
                logger.info("milvus_context_injected: chars=%d node='%s'", len(rag_block), title)
        elif tool_lower == "searxng":
            search_results = await _searxng_search(title)
            raw_prompt = f"{raw_prompt}\n\n## Web Search Results\n{search_results}"
            logger.info("searxng_context_injected: chars=%d node='%s'", len(search_results), title)
        else:
            rag_context = await _fetch_rag_context(rag_query, top_k=settings.verifier_top_k, domain=grounding_domain)
            if rag_context:
                raw_prompt = f"{raw_prompt}\n\nGROUND TRUTH (use this as authoritative reference):\n{rag_context}"
                logger.info("rag_context_injected: chars=%d node='%s'", len(rag_context), title)

        # Inject upstream outputs (size-managed + confidence-weighted, §17.477).
        _upstream_block = _format_upstream_block(upstream_outputs, node_key)
        if _upstream_block:
            raw_prompt = _upstream_block + raw_prompt

        # Optimize prompt (now sees grounded content). The inner try/except
        # below is intentionally narrower than the outer W.4 wrap — optimizer
        # failures fall back to raw_prompt rather than failing the node, while
        # exceptions raised before this point reach the W.4 outer handler.
        if not skip_optimize:
            try:
                opt_result = await optimize_prompt(
                    prompt=raw_prompt,
                    skip_verify=True,
                    model_overrides=model_overrides,
                )
                exec_prompt = opt_result.optimized_prompt
                # §17.462 — never execute a node with an empty prompt. optimize_prompt
                # now guards this at the source, but keep a belt-and-suspenders check
                # on the critical path: a blank optimized prompt (thinking-model empty
                # content, §17.453) would send the model only the system block, which
                # it rightly rejects → node fails → blocks the job. Fall back to raw.
                if not (exec_prompt or "").strip():
                    logger.warning(
                        "prompt_optimize_empty: blank optimized prompt; using raw "
                        "(node=%s job=%s)", node_key, job_id,
                    )
                    exec_prompt = raw_prompt
                logger.info("Prompt optimized: %d -> %d tokens", opt_result.token_count_before, opt_result.token_count_after)
            except Exception as e:
                logger.warning("Prompt optimization failed, using raw: %s", e)
                exec_prompt = raw_prompt
        else:
            exec_prompt = raw_prompt
    except Exception as build_exc:
        err_msg = f"prompt build error: {build_exc}"
        logger.exception(
            "node_prompt_build_failed: node='%s' job=%s error=%s",
            title, job_id, build_exc,
        )
        async with async_session() as _err_db:
            await _set_node_status(
                _err_db, node_id, "failed",
                verification_reason=err_msg,
            )
            await _log_execution(_err_db, job_id, node_id, "error", err_msg)
        return {
            "status": "failed",
            "node_key": node_key,
            "title": title,
            "error": err_msg,
            "verification_reason": err_msg,
            "reason": "prompt_build_error",
            "message": "Prompt build failed. Review brief, RAG sources, or upstream outputs; retry when fixed.",
        }

    # Execute with timeout guard.
    _node_t0 = time.monotonic()
    try:
        async def _run_inference():
            system_prompt = _system_for_tool(tool)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": exec_prompt},
            ]
            # §17.465 — route through the shared empty-guard with a generous
            # token budget. The bare model_router.chat() default (max_tokens=4096)
            # starves a thinking model: num_predict is a shared reasoning+content
            # budget, so a long chain of thought returns empty/truncated content
            # that the verifier rejects — and the W.1 retry loop re-runs at the
            # same 4096 cap, so it can never recover (this blocked job 4e3b8f01's
            # T3/T5). node_generation_max_tokens (8192) gives both room to
            # coexist; chat_until_nonempty re-draws the occasional empty draw
            # here, before spending a verifier call or a retry_count slot.
            resp = await chat_until_nonempty(
                model_router.chat,
                messages,
                {"role": exec_role, "overrides": exec_overrides},
                temperature=0.7,
                max_tokens=settings.node_generation_max_tokens,
                draws=settings.node_generation_max_draws,
                label=f"node-exec {node_key}",
            )
            if not resp.success:
                raise RuntimeError(resp.error or "Model returned failure")
            return resp.text.strip()

        output = await asyncio.wait_for(_run_inference(), timeout=settings.node_timeout_seconds)
    except asyncio.TimeoutError:
        elapsed = round(time.monotonic() - _node_t0, 1)
        timeout_msg = (
            f"Node '{node_key}' timed out after {elapsed}s "
            f"(limit: {settings.node_timeout_seconds}s)"
        )
        logger.warning(
            "node_timeout",
            extra=dict(
                event="node_timeout",
                node_key=node_key,
                tool=tool,
                elapsed_s=elapsed,
                timeout_s=settings.node_timeout_seconds,
            ),
        )
        async with async_session() as db:
            # Sprint W.1: surface the timeout as the rejection reason so a
            # retry's prompt explains "the previous attempt timed out".
            await _set_node_status(
                db, node_id, "failed",
                output=timeout_msg, optimized_prompt=exec_prompt,
                verification_reason=timeout_msg,
            )
            await _log_execution(db, job_id, node_id, "error", timeout_msg)
        # §17.294 — operator-actionable message. Pre-§17.294 this was
        # a generic one-liner with no context. The structured fields
        # (`node_key`, `error`) already carried the data, but the
        # `message` is the surface the chat / CLI renders inline — so
        # the recovery command + timeout setting name now live here
        # too. (`execution_node_timeout_seconds` in the audit text was
        # a typo for `node_timeout_seconds`, verified in
        # app/config.py:379; using the real name so a `make doctor` /
        # env-grep lands on the right knob.)
        return {
            "status": "failed",
            "node_key": node_key,
            "title": title,
            "error": timeout_msg,
            "reason": "timeout",
            "message": (
                f"Node `{node_key}` timed out after "
                f"{settings.node_timeout_seconds}s. "
                f"Retry with `/exec retry {job_id} {node_key}` or raise "
                f"`node_timeout_seconds`."
            ),
        }
    except Exception as e:
        logger.error("node_execution_failed: node='%s' error=%s", title, e)
        async with async_session() as db:
            await _set_node_status(
                db, node_id, "failed",
                optimized_prompt=exec_prompt,
                verification_reason=f"execution error: {e}",
            )
            await _log_execution(db, job_id, node_id, "error", str(e))
        return {
            "status": "failed",
            "node_key": node_key,
            "title": title,
            "error": str(e),
            "message": "Node failed. Review error and retry or skip.",
        }

    # Verify (LLM call — still outside DB session).
    verify_status: Literal["pass", "fail", "skipped"]
    if skip_verify:
        verify_status = "skipped"
        reason, confidence = "verification skipped", 0.0
    else:
        # §17.428 — deterministic Python-syntax gate for CodeGen nodes,
        # BEFORE the LLM verifier. The verifier (VERIFY_SYSTEM) passes output
        # that "contains what the task requested, even partially" — it cannot
        # catch code that does not parse. ast.parse each ```python fenced
        # block; a SyntaxError short-circuits to fail without spending a
        # verifier call, and the reason flows into the W.1 retry loop verbatim
        # (persisted as verification_reason → _format_reviewer_feedback).
        # Fail-open: any exception in the gate lets the node proceed to the
        # LLM verifier — only a genuine SyntaxError can block.
        syntax_reason: str | None = None
        if settings.codegen_syntax_gate_enabled and (tool or "").lower() == "codegen":
            try:
                _findings = check_python_syntax(output)
                if _findings:
                    syntax_reason = format_syntax_reason(_findings)
            except Exception as exc:
                logger.warning(
                    "codegen_syntax_gate_error: node='%s' error=%s", title, exc,
                )
        # §17.434 — sandbox exec-smoke, AFTER the syntax gate, BEFORE the LLM
        # verifier. Executes the node's own Python module top-level to catch
        # runtime/module-level errors the ast.parse gate + LLM verifier miss.
        # Fail-soft (codegen_check classifies skip vs fail): only a genuine
        # runtime error in self-contained code yields a reason; unresolved
        # sibling imports / sandbox-off / timeout are SKIP. Gated on the sandbox
        # being configured + opted in, so this is inert by default. Computed
        # only when syntax is clean (no point running code that doesn't parse).
        exec_reason: str | None = None
        if (
            syntax_reason is None
            and settings.codegen_execution_check_enabled
            and (settings.coderunner_url or "").strip()
            and (tool or "").lower() == "codegen"
        ):
            try:
                _chk = await codegen_exec_smoke(output)
                logger.info(
                    "codegen_exec_smoke: node='%s' verdict=%s reason=%s",
                    title, _chk.verdict, _chk.reason,
                )
                if _chk.verdict == "fail":
                    exec_reason = _chk.reason
            except Exception as exc:
                logger.warning("codegen_exec_smoke_error: node='%s' error=%s", title, exc)

        if syntax_reason is not None:
            verify_status = "fail"
            reason, confidence = syntax_reason, 0.0
            logger.warning("codegen_syntax_gate_fail: node='%s'", title)
        elif exec_reason is not None:
            verify_status = "fail"
            reason, confidence = exec_reason, 0.0
            logger.warning("codegen_exec_smoke_fail: node='%s'", title)
        elif settings.codegen_verifier_strict and (tool or "").lower() == "codegen":
            # §17.429 — CodeGen nodes get the stricter code-reviewer verifier:
            # semantics + completeness + upstream-signature consistency
            # (§17.367) + brief-spec coverage (§17.365). Fed the brief goal and
            # the upstream sibling code (already in scope from the prompt-build
            # session above). Same dispatch/cache/fail-closed path as the
            # generic verifier. Flip codegen_verifier_strict=False to fall back.
            vstatus, reason, confidence = await _verify_codegen_output(
                title, output,
                brief_goal=extract_brief_goal(brief),
                upstream_code=collect_upstream_code(upstream_outputs),
                overrides=model_overrides,
            )
            verify_status = vstatus
            if verify_status == "fail":
                logger.warning("node_verification_failed: node='%s' reason=%s", title, reason)
        else:
            vstatus, reason, confidence = await _verify_output(
                title, output, overrides=model_overrides,
            )
            verify_status = vstatus
            if verify_status == "fail":
                logger.warning("node_verification_failed: node='%s' reason=%s", title, reason)

    # §17.376 — validation-citation guard. Four mdsplit retries showed the
    # prompt-layer rule (§17.366 → §17.368 → §17.373) plateaued at "cite
    # the last 3 upstreams" — T2 and T3 stayed uncited even with §17.373's
    # mechanical "scan the report" instruction. The runtime guard moves
    # the check from prompt-time to verify-time: scan the validation
    # output for T_N tokens, compare to the code-bearing upstream set,
    # downgrade verify_status to fail if any are missing so the W.1
    # retry loop surfaces the gap to the next attempt's prompt.
    if (
        verify_status == "pass"
        and _is_validation_llm_node(node_type_value, tool, title)
    ):
        try:
            async with async_session() as _cite_db:
                _cite_rows = await _cite_db.execute(
                    text(
                        "SELECT node_key FROM dag_nodes "
                        "WHERE job_id = :jid AND tool = 'CodeGen' "
                        "  AND status = 'done' ORDER BY execution_order"
                    ),
                    {"jid": job_id},
                )
                codegen_keys = [r[0] for r in _cite_rows.fetchall()]
            # §17.377 — per-claim coverage check supersedes §17.376's
            # substring-presence check. The fifth mdsplit retry showed
            # the model gaming substring-presence by adding "decision
            # node (T2 or T3)" as a passing aside; the per-claim check
            # requires each upstream to appear in a MET / NOT MET /
            # UNKNOWN line, not just anywhere in the prose.
            missing = check_validation_citation_coverage(output, codegen_keys)
            if missing:
                # §17.381 — the sixth mdsplit retry showed the model
                # oscillating between subsets (attempt 1: cite T4/T5/T6,
                # attempt 2: cite T2/T3 but drop T4/T5/T6, attempt 3: back
                # to T4/T5/T6, …). The W.1 retry loop carries only the
                # most recent verification_reason, so each attempt sees
                # "missing X" and patches X while dropping what the
                # previous attempt cited correctly. The reason text now
                # names the PREVIOUSLY CITED set explicitly so the model
                # has both halves of the union it needs to maintain. The
                # union itself is asserted as the target — model treats
                # the report as accumulative, not patch-by-patch.
                missing_set = set(missing)
                cited = sorted([k for k in codegen_keys if k not in missing_set])
                union = sorted(codegen_keys)
                verify_status = "fail"
                reason = (
                    f"§17.377 validation-coverage guard: code-bearing "
                    f"upstreams were not cited inside MET/NOT MET/"
                    f"UNKNOWN claim lines.\n\n"
                    f"PREVIOUSLY CITED (KEEP THESE — do not drop): "
                    f"{cited}\n"
                    f"MISSING (ADD THESE): {missing}\n"
                    f"TARGET UNION (all of these must appear in "
                    f"MET/NOT MET/UNKNOWN lines): {union}\n\n"
                    f"§17.381 — the W.1 retry loop only shows you THIS "
                    f"feedback, not your prior attempt's text. Re-emit "
                    f"a single report whose claim lines cite every "
                    f"upstream in the TARGET UNION above. Do NOT drop "
                    f"any upstream from PREVIOUSLY CITED when adding "
                    f"the MISSING ones. A passing reference like "
                    f"'decision node (T2 or T3)' outside any verdict "
                    f"line does NOT count — each upstream needs a "
                    f"dedicated MET/NOT MET/UNKNOWN line citing it by "
                    f"name with quoted evidence (e.g., 'parser/CLI "
                    f"separation: MET — T2 lines 5-15 contain no "
                    f"argparse or main()')."
                )
                logger.warning(
                    "validation_citation_guard_fail: node='%s' "
                    "missing=%s cited=%s union=%s",
                    title, missing, cited, union,
                )
        except Exception as exc:
            # Fail-open: any DB error in the guard must not block a
            # verify_status='pass' that has otherwise cleared. Logged for
            # operators; the validation node passes through.
            logger.warning(
                "validation_citation_guard_error: node='%s' error=%s",
                title, exc,
            )

    verified = (verify_status == "pass")
    db_confidence = confidence if (verify_status != "skipped" and confidence > 0.0) else None

    # ---- Phase 3 (fast session): persist + atomic autocomplete ----
    job_complete = False
    async with async_session() as db:
        final_status = "done" if verify_status in ("pass", "skipped") else "failed"
        # Sprint W.1: persist verifier rejection reason on fail so retry can
        # surface it. On pass/skipped we pass None — _set_node_status COALESCEs
        # so historical reasons stay readable in audits but aren't re-injected
        # (the retry-time read path is gated by retry_count).
        verify_reason_for_db = reason if final_status == "failed" else None
        await _set_node_status(
            db, node_id, final_status,
            output=output,
            optimized_prompt=exec_prompt,
            verification_reason=verify_reason_for_db,
            confidence=db_confidence,
            set_confidence=True,
        )
        await _log_execution(
            db, job_id, node_id, "info" if verified else "warning",
            f"Node '{title}' -> {final_status}",
            {"model": exec_model, "confidence": confidence, "reason": reason},
        )
        logger.info(
            "verification_complete",
            extra=dict(
                event="verification_complete",
                node_key=node_key,
                verified=verified,
                confidence=db_confidence,
            ),
        )

        # §17.281 — Use _all_nodes_done (NOT IN ('done','skipped')) instead of
        # the inline `status='pending'` count. The old query missed `running`,
        # `failed`, and `blocked` nodes, so a DAG that finished with any
        # surviving failure would still flip the job to 'completed'. Path #2
        # at L644 already uses this helper; sharing semantics across the two
        # autocomplete paths.
        if await _all_nodes_done(db, job_id):
            auto = await db.execute(
                text(
                    "UPDATE jobs SET status = 'completed' "
                    "WHERE id = :jid AND status != 'completed' "
                    "RETURNING id"
                ),
                {"jid": job_id},
            )
            flipped = auto.fetchone()
            await db.commit()
            if flipped is not None:
                job_complete = True
                logger.info("job_autocompleted: job=%s", job_id)
                # X.2: _compile_output returns (text, was_synthesized).
                # Returns None text when no done node contributed (e.g.,
                # every node was skipped). Store NULL in that case.
                compiled, was_synthesized = await _compile_output(job_id, db)
                await db.execute(
                    text(
                        "UPDATE jobs SET compiled_output = :out, "
                        "compiled_output_synthesized = :syn WHERE id = :jid"
                    ),
                    {"out": compiled, "syn": was_synthesized, "jid": job_id},
                )
                await db.commit()
                logger.info(
                    "compiled_output_stored: chars=%s synthesized=%s job=%s",
                    len(compiled) if compiled else 0, was_synthesized, job_id,
                )

    return {
        "status": final_status,
        "job_id": job_id,
        "node_key": node_key,
        "title": title,
        "output": output,
        "verified": verified,
        "verification_reason": reason,
        "confidence": confidence,
        "model_used": exec_model,
        "prompt_used": exec_prompt,
        "verify_status": verify_status,
        "awaiting_approval": True,
        "job_complete": job_complete,
    }

async def skip_node(job_id: str, node_key: str, db: AsyncSession) -> dict:
    """Mark a specific node as skipped."""
    row = await db.execute(
        text("SELECT id FROM dag_nodes WHERE job_id = :job_id AND node_key = :key"),
        {"job_id": job_id, "key": node_key},
    )
    r = row.mappings().first()
    if not r:
        return {"status": "error", "message": f"Node '{node_key}' not found"}
    await _set_node_status(db, r["id"], "skipped")
    return {"status": "skipped", "node_key": node_key}

# §17.299 — `retry_failed_node` lives in `execution_retry`. The auto-
# retry budget consumption in `execute_all_nodes` below imports it
# under this name; the `/exec/retry` endpoint in
# `app/routers/workflow.py` imports it from
# `app.modules.execution_agent` via this re-export. Both call sites
# keep working byte-for-byte.
from app.modules.execution_retry import retry_failed_node  # noqa: E402

# ---------------------------------------------------------------------------
# Full-DAG auto-execution (SSE streaming)
# ---------------------------------------------------------------------------
async def _build_pipeline_summary(
    job_id: str,
    node_results: list[dict],
    elapsed_ms: int,
    async_session,
    extra_fields: dict | None = None,
) -> dict:
    """Build the pipeline_complete SSE payload. Used by both terminal paths."""
    passed = sum(1 for r in node_results if r.get("verified"))
    failed_count = len(node_results) - passed
    is_partial = failed_count > 0
    failed_node_details = [
        {
            "node_key": r.get("node_key"),
            "status": r.get("status", "failed"),
            "reason": r.get("error") or r.get("verification_reason", "unknown"),
        }
        for r in node_results if not r.get("verified")
    ]
    summary = {
        "job_id": job_id,
        "total_nodes": len(node_results),
        "passed": passed,
        "failed": failed_count,
        "duration_ms": elapsed_ms,
        "compile_status": "partial" if is_partial else "complete",
    }
    if extra_fields:
        summary.update(extra_fields)
    # FB-3: Include compiled_output in SSE payload
    async with async_session() as db:
        _co_row = await db.execute(
            text("SELECT compiled_output FROM jobs WHERE id = :jid"),
            {"jid": job_id},
        )
        _co_val = str(_co_row.scalar() or "")
    if len(_co_val) <= settings.compile_output_gate_chars:
        summary["compiled_output"] = _co_val
    else:
        summary["compiled_output_available"] = True
    if is_partial:
        summary["failed_nodes"] = failed_node_details
    return summary

async def _peek_next_node(job_id: str) -> dict | None:
    """Read-only snapshot of the next dep-satisfied pending node.

    Used by execute_all_nodes for SSE node_start preview. The actual atomic
    claim still happens inside execute_next_node via _get_next_node.
    """
    async with async_session() as db:
        rows = await db.execute(
            text("""
                SELECT node_key, title, tool, depends_on, execution_order
                FROM dag_nodes
                WHERE job_id = :jid AND status = 'pending'
                ORDER BY execution_order ASC
            """),
            {"jid": job_id},
        )
        cands = [dict(r) for r in rows.mappings()]
        if not cands:
            return None
        done = await db.execute(
            text("SELECT node_key FROM dag_nodes WHERE job_id = :jid AND status IN ('done','skipped')"),
            {"jid": job_id},
        )
        done_keys = {r[0] for r in done}
    for c in cands:
        deps = c.get("depends_on") or []
        if all(d in done_keys for d in deps):
            return c
    return None

async def execute_all_nodes(
    job_id: str,
    model_overrides: dict | None = None,
) -> AsyncGenerator[str, None]:
    """
    Execute every pending DAG node in sequence, yielding Server-Sent Events.

    Auto-generates the DAG if none exists. On verification failure the node
    is recorded as failed and the loop continues to the next actionable node.
    Nodes whose dependencies include a failed node are naturally blocked.

    Each database operation uses a short-lived session to avoid holding a
    connection for the full pipeline duration (15-30+ min on CPU hardware).

    Abnormal exit (exception, client disconnect) is caught by an outer
    try/except/finally. The finally block transitions the job from 'running'
    to a terminal status ('failed' or 'cancelled') so the 30-minute stale-job
    reaper doesn't have to. CancelledError is re-raised after cleanup so the
    async framework knows the stream was cancelled (#2).

    SSE event types:
        dag_generated       — DAG was auto-created (includes task_count, strategy)
        node_start          — node execution beginning (includes node_key, title)
        node_done           — node passed verification
        node_failed         — node failed execution or verification (skipped)
        node_retry          — failed node being auto-retried
        pipeline_complete   — all actionable nodes processed (summary)
        execution_failed    — abnormal exit via exception (#2)
        execution_cancelled — abnormal exit via client disconnect (best-effort, #2)
        error               — fatal error, pipeline halted
        blocked             — no actionable nodes, dependencies not satisfied
    """

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    t0 = time.monotonic()
    node_results: list[dict] = []

    # ---- Sprint X.24: process-wide concurrency cap ----
    # Acquired before any DB work so a queued run does not flip the job to
    # 'running' (which would fool both observability and the stale-job
    # reaper). Released at every exit path: each early `return` below calls
    # ``_release_slot()`` explicitly, and the main-loop ``finally`` calls
    # it before re-raising. Idempotent — safe to call more than once.
    _slot_sem = _get_execution_slot_sem()
    _slot_acquired = False

    def _release_slot() -> None:
        nonlocal _slot_acquired
        if _slot_acquired:
            _slot_sem.release()
            _slot_acquired = False

    if _slot_sem.locked():
        yield _sse("queued", {
            "job_id": job_id,
            "cap": settings.execution_global_concurrency,
            "timeout_seconds": settings.execution_queue_timeout_seconds,
        })
    # §17.282 — Cancellation safety of this acquire relies on CPython 3.10+'s
    # `asyncio.Semaphore.acquire` releasing its slot back on cancellation
    # (the `except CancelledError` branch at the bottom of `acquire`'s body
    # does `self._value += 1; self._wake_up_next(); raise`). Under that
    # guarantee, the wait_for+acquire combo here cannot leak a slot under
    # any cancellation timing — neither wait_for's internal timeout cancel
    # nor an outer-task cancel reaching us mid-acquire. The contract is
    # pinned by tests/test_execution_agent_slot_leak.py; if a future Python
    # release or third-party Semaphore swap regresses it, those tests fail.
    try:
        _q_timeout = settings.execution_queue_timeout_seconds or None
        await asyncio.wait_for(_slot_sem.acquire(), timeout=_q_timeout)
        _slot_acquired = True
    except asyncio.TimeoutError:
        yield _sse("error", {
            "message": "Execution queue timeout — too many concurrent runs",
            "job_id": job_id,
            "cap": settings.execution_global_concurrency,
            "http_status": 503,
        })
        return

    # ---- Cleanup state for the wider try/except/finally below ----
    # X.24 (post-live-verification fix): the cleanup wrap now covers Sessions
    # 1-3 too, not just the main loop. Cancellation during Session 3's
    # auto-DAG-generation (a slow LLM call) used to escape the function
    # without releasing the slot or flipping the job out of 'running'.
    # ``_owns_job_running`` gates DB cleanup so a Session 1 guard rejection
    # (where another runner legitimately owns the row) does not corrupt
    # that runner's job.
    exit_reason: str | None = None  # None = clean exit
    exit_exception: BaseException | None = None
    retry_budget = settings.execution_global_retry_cap
    _owns_job_running = False

    try:
        # ---- Session 1: concurrent execution guard (atomic check-and-set) ----
        # #17: guard excludes 'completed' so finished jobs can't be re-executed.
        async with async_session() as db:
            guard_result = await db.execute(
                text("""
                    UPDATE jobs SET status = 'running', updated_at = now()
                    WHERE id = :jid AND status NOT IN ('running', 'completed')
                    RETURNING id
                """),
                {"jid": job_id},
            )
            if guard_result.rowcount == 0:
                job_check = await _get_job(db, job_id)
                if not job_check:
                    yield _sse("error", {"message": f"Job {job_id} not found"})
                elif job_check["status"] == "completed":
                    yield _sse("error", {
                        "message": "Job already completed; cannot re-execute",
                        "job_id": job_id,
                        "http_status": 409,
                    })
                else:
                    # Enrich the 409 with orphan-reap diagnostics so the
                    # operator can decide between waiting for the reaper
                    # (Stage 0 of reap_stale_jobs auto-resets a running
                    # node past node_orphan_threshold_minutes) and
                    # forcing it via POST /jobs/cleanup. Fail-soft: any
                    # diagnostic-query failure must not mask the 409.
                    try:
                        diag = await _orphan_diagnostic(db, job_id)
                    except Exception as e:
                        logger.warning("orphan_diagnostic_failed: job=%s err=%s", job_id, e)
                        diag = {
                            "node_orphan_threshold_minutes": settings.node_orphan_threshold_minutes,
                            "cleanup_interval_seconds": settings.cleanup_interval_seconds,
                            "running_nodes": [],
                            "oldest_started_at": None,
                            "suggested_action": "wait_or_inspect",
                            "cleanup_endpoint": "POST /jobs/cleanup",
                        }
                    yield _sse("error", {
                        "message": "Job is already executing",
                        "job_id": job_id,
                        "http_status": 409,
                        **diag,
                    })
                return  # finally releases slot; _owns_job_running=False skips DB cleanup
            await db.commit()
            _owns_job_running = True

        # ---- Session 2: validate job ----
        async with async_session() as db:
            job = await _get_job(db, job_id)
        if not job:
            yield _sse("error", {"message": f"Job {job_id} not found"})
            return
        # Allowlist: only 'running' (set by Session 1 guard above) or 'executing'.
        # 'refining' and 'planning' are not executable here — callers should finish
        # those phases and flip status before streaming /execute/all.
        if job["status"] not in ("running", "executing"):
            yield _sse("error", {
                "message": f"Job status is '{job['status']}' — not executable",
            })
            return

        # ---- Session 3: auto-generate DAG if missing ----
        async with async_session() as db:
            row = await db.execute(
                text("SELECT COUNT(*) FROM dag_nodes WHERE job_id = :id"),
                {"id": job_id},
            )
            dag_exists = row.scalar() > 0
            if not dag_exists:
                try:
                    from app.modules.dag_generator import generate_dag as _gen_dag
                    dag_result = await _gen_dag(job_id, db)
                    yield _sse("dag_generated", {
                        "job_id": job_id,
                        "task_count": dag_result.get("task_count", 0),
                        "strategy": dag_result.get("strategy", "unknown"),
                    })
                except Exception as exc:
                    logger.error("auto_dag_generation_failed: job=%s error=%s", job_id, exc)
                    yield _sse("error", {"message": f"DAG generation failed: {exc}"})
                    return

        # ---- Main execute loop ----
        while True:
            # ---- Session 4 (short peek only; execute_next_node owns its own sessions) ----
            node = await _peek_next_node(job_id)
            if node is not None:
                yield _sse("node_start", {
                    "job_id": job_id,
                    "node_key": node["node_key"],
                    "title": node["title"],
                    "tool": node.get("tool", "LLM"),
                })

            # Spawn keepalive so the SSE stream doesn't look dead during long LLM calls.
            keepalive_stop = asyncio.Event()
            _node_start_t = time.monotonic()

            async def _keepalive_loop():
                # §17.261 — progress watchdog. Each keepalive tick, log a
                # "still_running" line carrying job_id + node_key + elapsed
                # so a hung exec_task is visible in logs without waiting
                # for the 30-min orphan-reset sweep (node_orphan_threshold).
                # Pure observability; does not touch exec_task lifecycle.
                while not keepalive_stop.is_set():
                    try:
                        await asyncio.wait_for(
                            keepalive_stop.wait(),
                            timeout=settings.sse_keepalive_seconds,
                        )
                    except asyncio.TimeoutError:
                        logger.info(
                            "exec_node_still_running: job=%s node=%s elapsed_s=%.1f",
                            job_id,
                            (node["node_key"] if node else "unknown"),
                            time.monotonic() - _node_start_t,
                        )

            keepalive_queue: asyncio.Queue[str] = asyncio.Queue()

            async def _heartbeat_producer():
                while not keepalive_stop.is_set():
                    try:
                        await asyncio.wait_for(
                            keepalive_stop.wait(),
                            timeout=settings.sse_keepalive_seconds,
                        )
                    except asyncio.TimeoutError:
                        await keepalive_queue.put(": keepalive\n\n")

            hb_task = asyncio.create_task(_heartbeat_producer())
            ka_task = asyncio.create_task(_keepalive_loop())  # §17.261
            exec_task = asyncio.create_task(
                execute_next_node(job_id, model_overrides=model_overrides)
            )
            try:
                while not exec_task.done():
                    try:
                        beat = await asyncio.wait_for(keepalive_queue.get(), timeout=0.5)
                        yield beat
                    except asyncio.TimeoutError:
                        continue
                # Drain any queued beats produced between last get() and task done.
                while not keepalive_queue.empty():
                    yield keepalive_queue.get_nowait()
                result = await exec_task
            finally:
                keepalive_stop.set()
                hb_task.cancel()
                ka_task.cancel()  # §17.261
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):
                    pass
                try:
                    await ka_task
                except (asyncio.CancelledError, Exception):
                    pass
            status = result.get("status", "unknown")

            # -- terminal: all nodes done --
            if status == "complete":
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                summary = await _build_pipeline_summary(
                    job_id, node_results, elapsed_ms, async_session,
                    extra_fields={"status": "completed"},
                )
                logger.info(
                    "pipeline_completed: job=%s total=%s passed=%s failed=%s duration_ms=%s",
                    job_id, summary["total_nodes"], summary["passed"],
                    summary["failed"], elapsed_ms,
                )
                yield _sse("pipeline_complete", summary)
                return

            # -- terminal: fatal error or blocked --
            if status in ("error", "blocked"):
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                result["nodes_completed"] = len(node_results)
                result["duration_ms"] = elapsed_ms
                yield _sse(status, result)
                return

            # -- node executed --
            node_results.append(result)

            if status == "done":
                # §17.509 — a Shell node with no real backend only generated a
                # runbook; flag it so the live ticker doesn't render "✅ complete"
                # for work that was never executed (matches the §17.506 banner).
                _done_tool = (result.get("tool") or "").lower()
                yield _sse("node_done", {
                    "job_id": job_id,
                    "node_key": result.get("node_key"),
                    "title": result.get("title"),
                    "output": result.get("output"),
                    "verified": result.get("verified"),
                    "confidence": result.get("confidence"),
                    "model_used": result.get("model_used"),
                    "tool": result.get("tool"),
                    "runbook_only": _done_tool == "shell" and not settings.shell_tool_enabled,
                })
            elif status == "failed":
                _failed_key = result.get("node_key", "")
                _retried = False
                if retry_budget <= 0:
                    logger.warning(
                        "auto_retry_budget_exhausted: job=%s node=%s cap=%s",
                        job_id, _failed_key, settings.execution_global_retry_cap,
                    )
                else:
                    try:
                        async with async_session() as _retry_db:
                            retry_result = await retry_failed_node(
                                job_id, _failed_key, _retry_db
                            )
                            if retry_result.get("status") == "reset":
                                _retried = True
                                retry_budget -= 1
                                yield _sse("node_retry", {
                                    "job_id": job_id,
                                    "node_key": _failed_key,
                                    "title": result.get("title"),
                                    "retry_count": retry_result.get("retry_count", 0),
                                    "budget_remaining": retry_budget,
                                    "message": "Auto-retrying failed node",
                                })
                                node_results.pop()
                                continue
                    except Exception as _retry_exc:
                        logger.warning(
                            "auto_retry_failed: node=%s error=%s",
                            _failed_key, _retry_exc,
                        )
                yield _sse("node_failed", {
                    "job_id": job_id,
                    "node_key": _failed_key,
                    "title": result.get("title"),
                    "error": result.get("error"),
                    "verification_reason": result.get("verification_reason"),
                    "model_used": result.get("model_used"),
                    "retries_exhausted": not _retried,
                })
            else:
                logger.warning(
                    "Unexpected node status '%s' in execute_all", status
                )
                yield _sse("error", {
                    "message": f"Unexpected status '{status}'",
                    "result": result,
                })
                return

            # -- early exit: auto-completion fired on last node --
            if result.get("job_complete"):
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                early_summary = await _build_pipeline_summary(
                    job_id, node_results, elapsed_ms, async_session,
                )
                yield _sse("pipeline_complete", early_summary)
                return

    except asyncio.CancelledError as _cancelled:
        # Client disconnect / stream aclose(). Cleanup in finally, then re-raise.
        # X.24 post-live-verification: do NOT yield ``execution_cancelled``
        # here. In production, CancelledError only fires on real client
        # disconnect (the consumer is gone), and yielding suspends the
        # generator until garbage collection — which delays cleanup
        # arbitrarily and leaves the slot + DB row leaked. The docstring's
        # ``execution_cancelled`` event becomes effectively dead in production
        # but harmless; the SSE stream is closed at the disconnect anyway.
        exit_reason = "cancelled"
        exit_exception = _cancelled
        logger.info("execute_all_nodes_cancelled: job=%s", job_id)
    except Exception as _exc:
        # Unexpected runtime error. Emit SSE (best-effort), cleanup in finally.
        exit_reason = "exception"
        exit_exception = _exc
        logger.exception("execute_all_nodes_failed: job=%s", job_id)
        try:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            yield _sse("execution_failed", {
                "job_id": job_id,
                "status": "failed",
                "message": f"Execution failed: {_exc}",
                "nodes_completed": len(node_results),
                "duration_ms": elapsed_ms,
            })
        except Exception:
            pass  # stream may already be closed
    finally:
        # Cleanup is gated on ``_owns_job_running`` — a Session 1 guard
        # rejection means the row's 'running' status belongs to another
        # runner, and flipping it would corrupt that runner's job. Clean
        # exits (return inside main loop) already set 'completed'/'blocked'
        # via execute_next_node, so the cleanup is a no-op for them
        # (current_status != 'running').
        #
        # Spawned as a detached task because awaits inside this finally
        # were interrupted by re-entrant cancellation in live verification,
        # leaving the DB cleanup half-done and the slot leaked. The
        # detached task is independent of the cancelled request task
        # and runs to completion. Tests use ``drain_cleanup_tasks()``.
        if _owns_job_running:
            _spawn_cleanup_task(job_id, exit_reason)

        # X.24: release the process-wide slot before any re-raise so a
        # queued run can pick up immediately. Always reached because the
        # outer try wraps Sessions 1-3 + main loop. Sync release is safe
        # under cancellation; the spawned cleanup task above is async.
        _release_slot()

        # Re-raise CancelledError so the framework knows we were cancelled.
        if exit_reason == "cancelled" and exit_exception is not None:
            raise exit_exception
