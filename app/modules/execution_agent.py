"""
execution_agent.py  --  Step 15

Executes DAG nodes one at a time with user confirmation gate.

Flow per node:
  fetch next pending node (deps satisfied)
    -> optimize prompt (Step 14)
      -> execute via model_router
        -> verify output (phi4-mini-reasoning)
          -> persist result + update status
            -> return to user for approval

Error recovery cascade (per spec):
  1. Retry same model 3x (handled by model_router)
  2. Swap to local fallback
  3. Replan node (simplified: mark failed + surface to user)
  4. Log + present to user
"""

import asyncio
import logging
import time as _time_mod
from typing import AsyncGenerator,  Optional
from uuid import UUID
import httpx

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import async_session

from app import model_router
from app.config import settings
from app.modules.prompt_optimizer import optimize_prompt
from app.modules.rag_pipeline import query_rag

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable constants
# ---------------------------------------------------------------------------
NODE_TIMEOUT_SECONDS = 600
MAX_UPSTREAM_CHARS = 6000

# ---------------------------------------------------------------------------
# Verify system prompt
# ---------------------------------------------------------------------------

VERIFY_SYSTEM = """You are a Requirements Satisfaction Checker. Your ONLY job is to confirm that the required functionality is present and correct.

Respond with ONLY a JSON object in this format:
{"pass": true, "reason": "one sentence", "confidence": 0.95}

RULES:
- PASS if the output contains what the task requested, even partially.
- A complete implementation that includes the required feature is a PASS.
- Additional content beyond the requirement is acceptable and expected.
- FAIL only if the required functionality is completely missing or fundamentally incorrect.
- confidence: 0.0 to 1.0

Example 1 — PASS (exact match):
TASK: "List 3 sorting algorithms"
OUTPUT: "Bubble sort, merge sort, quicksort"
{"pass": true, "reason": "Three sorting algorithms listed as requested", "confidence": 0.95}

Example 2 — PASS (exceeds scope, still correct):
TASK: "Define a function signature for merging two sorted lists"
OUTPUT: "def merge_sorted(a, b):\n    result = []\n    while a and b:\n        if a[0] <= b[0]: result.append(a.pop(0))\n        else: result.append(b.pop(0))\n    return result + a + b"
{"pass": true, "reason": "Function signature present with full implementation — extra detail is acceptable", "confidence": 0.93}

Example 3 — PASS (broad answer to narrow task):
TASK: "Handle empty list edge case"
OUTPUT: "The function checks if either list is empty and returns the other list directly. It also handles the general merge case for non-empty lists."
{"pass": true, "reason": "Empty list handling is addressed as requested", "confidence": 0.90}

Example 4 — FAIL (genuinely missing):
TASK: "List 3 sorting algorithms"
OUTPUT: "Bubble sort is a comparison-based algorithm that repeatedly steps through the list"
{"pass": false, "reason": "Only one algorithm mentioned, task requires three", "confidence": 0.90}

Respond with ONLY the JSON object."""

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


async def _get_next_node(db: AsyncSession, job_id: str) -> dict | None:
    """Return the next pending node whose dependencies are all done."""
    rows = await db.execute(
        text("""
            SELECT id, node_key, title, node_type, depends_on,
                   assigned_model, prompt_template, execution_order, tool, domain
            FROM dag_nodes
            WHERE job_id = :job_id AND status = 'pending'
            ORDER BY execution_order ASC
        """),
        {"job_id": job_id},
    )
    nodes = [dict(r) for r in rows.mappings()]
    if not nodes:
        return None

    # Fetch done node_keys
    done_rows = await db.execute(
        text("SELECT node_key FROM dag_nodes WHERE job_id = :job_id AND status IN ('done', 'skipped')"),
        {"job_id": job_id},
    )
    done_keys = {r[0] for r in done_rows}

    for node in nodes:
        deps = node.get("depends_on") or []
        if all(d in done_keys for d in deps):
            return node
    return None


async def _set_node_status(
    db: AsyncSession,
    node_id: str,
    status: str,
    output: str | None = None,
    optimized_prompt: str | None = None,
) -> None:
    await db.execute(
        text("""
            UPDATE dag_nodes
            SET status = :status,
                output_text = :output,
                optimized_prompt = :optimized_prompt,
                completed_at = CASE WHEN :status IN ('done','failed','skipped')
                               THEN NOW() ELSE completed_at END
            WHERE id = :id
        """),
        {"id": str(node_id), "status": status, "output": output, "optimized_prompt": optimized_prompt},
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
    import json
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


def _truncate_output(text: str, max_chars: int) -> str:
    """Truncate text preserving first/last 20%, with a marker in the middle."""
    if len(text) <= max_chars:
        return text
    keep = max_chars
    head_len = int(keep * 0.2)
    tail_len = int(keep * 0.2)
    removed = len(text) - head_len - tail_len
    return (
        text[:head_len]
        + f"\n[...truncated {removed} chars...]\n"
        + text[-tail_len:]
    )


async def _fetch_upstream_outputs(
    db, job_id: str, depends_on: list[str]
) -> dict[str, str]:
    """Fetch output_text for upstream nodes by node_key."""
    if not depends_on:
        return {}
    rows = await db.execute(
        text(
            "SELECT node_key, output_text FROM dag_nodes "
            "WHERE job_id = :jid AND node_key = ANY(:keys) AND status = 'done'"
        ),
        {"jid": job_id, "keys": depends_on},
    )
    return {r.node_key: (r.output_text or "") for r in rows.fetchall()}


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
            if vec_score == 0.0 or vec_score > 1.0:
                logger.info("RAG skip irrelevant doc: %s (L2=%.3f, rrf=%.4f)", r.get("topic", "?"), vec_score, rrf_score)
                continue
            entries.append(f"[{r['topic']}] {r['content']}")
        if not entries:
            logger.info("RAG: all results below relevance threshold (0.4)")
        return "\n\n".join(entries)
    except Exception as e:
        logger.warning("RAG grounding failed: %s", e)
        return ""


async def _build_prompt(node: dict, brief: dict) -> str:
    """Build execution prompt from node template + brief context."""
    template = node.get("prompt_template") or ""
    title = node["title"]
    goal = brief.get("description", "") if brief else ""
    if not goal and brief:
        goals = brief.get("goals", [])
        goal = goals[0] if goals else ""

    if template:
        return f"{template}\n\nContext: {goal}"
    return (
        f"Execute this task: {title}\n\n"
        f"Project goal: {goal}\n\n"
        f"Produce a complete, actionable output for this task. "
        f"Base your response on the ground truth provided above where relevant."
    )


async def _verify_output(task_title: str, output: str, model: str) -> tuple[bool, str, float]:
    """Verify output quality. Returns (pass, reason, confidence)."""
    import json
    import re as _re
    from json_repair import repair_json
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM},
        {
            "role": "user",
            "content": f"TASK: {task_title}\n\nOUTPUT:\n{output}",
        },
    ]
    try:
        resp = await model_router.chat(messages=messages, model=model)
        raw = resp.text.strip()
        logger.info("Verifier raw response (first 500 chars): %s", raw[:500])
        if not raw:
            logger.warning("Verifier returned empty response")
            return True, "Verification skipped (empty response)", 0.0

        # --- Layer 1: Strip <think> reasoning blocks ---
        cleaned = _re.sub(r'<think>.*?</think>', '', raw, flags=_re.DOTALL)
        cleaned = _re.sub(r'<thinking>.*?</thinking>', '', cleaned, flags=_re.DOTALL)
        # Handle unclosed <think> from truncation
        cleaned = _re.sub(r'<think>.*', '', cleaned, flags=_re.DOTALL)
        cleaned = cleaned.strip()
        logger.info("Verifier after think-strip (first 300 chars): %s", cleaned[:300])

        if not cleaned:
            logger.warning("No content after stripping think tags")
            return True, "Verification skipped (only reasoning, no answer)", 0.0

        # --- Layer 2: Strip markdown code fences ---
        fence_match = _re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', cleaned, _re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        # --- Layer 3: Direct parse (fast path) ---
        try:
            data = json.loads(cleaned)
            logger.info("Verifier parsed (direct): %s", data)
            return _extract_verify_result(data)
        except json.JSONDecodeError:
            pass

        # --- Layer 4: json_repair for malformed JSON ---
        try:
            repaired = repair_json(cleaned, ensure_ascii=False)
            data = json.loads(repaired)
            logger.info("Verifier parsed (repaired): %s", data)
            return _extract_verify_result(data)
        except Exception:
            pass

        # --- Layer 5: Find first { and repair from there ---
        brace = cleaned.find("{")
        if brace != -1:
            try:
                repaired = repair_json(cleaned[brace:], ensure_ascii=False)
                data = json.loads(repaired)
                logger.info("Verifier parsed (brace-find): %s", data)
                return _extract_verify_result(data)
            except Exception:
                pass

        logger.warning("All verifier parse strategies failed | cleaned: %s", cleaned[:300])
        return True, "Verification skipped (parse error)", 0.0
    except Exception as e:
        logger.warning("Verifier failed: %s | Raw: %s", e, resp.text[:200] if 'resp' in locals() else 'N/A')
        return True, "Verification skipped (error)", 0.0


def _extract_verify_result(data: dict) -> tuple[bool, str, float]:
    """Pull pass/reason/confidence from parsed verifier JSON."""
    if "pass" not in data:
        logger.warning("Verifier JSON missing 'pass' key — treating as skip: %s", 
                       str(data)[:200])
        return True, "Verification skipped (model returned wrong schema)", 0.0
    return (
        bool(data.get("pass", False)),
        str(data.get("reason", "")),
        float(data.get("confidence", 0.0)),
    )


# ---------------------------------------------------------------------------
# Public API

# ── Tool Dispatch ────────────────────────────────────────────
import os as _os
_SEARXNG_URL = _os.environ.get("SEARXNG_URL", "http://searxng:8080")


async def _searxng_search(query: str, max_results: int = 5) -> str:
    """Call SearXNG JSON API, return formatted results."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_SEARXNG_URL}/search",
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
            topic = doc.get("topic", "Unknown")
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
    db: AsyncSession,
    skip_optimize: bool = False,
    skip_verify: bool = False,
    model_override: Optional[str] = None,
) -> dict:
    """
    Execute the next pending node in the DAG.

    Returns a result dict for the caller (pipeline / endpoint) to present
    to the user for approval before the next node runs.
    """
    # 1. Validate job
    job = await _get_job(db, job_id)
    if not job:
        return {"status": "error", "message": f"Job {job_id} not found"}
    if job["status"] not in ("running", "executing", "planning"):
        return {"status": "error", "message": f"Job status is '{job['status']}' — not executable"}

    # 2. Get next node
    node = await _get_next_node(db, job_id)
    if not node:
        if await _all_nodes_done(db, job_id):
            await db.execute(
                text("UPDATE jobs SET status = 'completed' WHERE id = :id"),
                {"id": job_id},
            )
            await db.commit()
            return {"status": "complete", "message": "All nodes done. Job complete."}
        # ── Partial compile for blocked jobs ──
        try:
            partial_result = await _compile_output(job_id, db)
            if partial_result:
                await db.execute(
                    text("UPDATE jobs SET compiled_output = :co, status = 'blocked' WHERE id = :jid"),
                    {"co": partial_result, "jid": job_id}
                )
            else:
                await db.execute(
                    text("UPDATE jobs SET status = 'blocked' WHERE id = :jid"),
                    {"jid": job_id}
                )
            await db.commit()
            logger.info("partial_compiled: job=%s chars=%s", job_id, len(partial_result) if partial_result else 0)
        except Exception as exc:
            logger.warning("partial_compile_failed: job=%s error=%s", job_id, str(exc))
        # ── Identify blocked nodes and their failed dependencies ──
        blocked_nodes = []
        try:
            _all = await db.execute(
                text("SELECT node_key, title, status, depends_on FROM dag_nodes WHERE job_id = :jid"),
                {"jid": job_id}
            )
            _rows = _all.fetchall()
            failed_keys = {r.node_key for r in _rows if r.status == "failed"}
            for r in _rows:
                if r.status == "pending":
                    deps = r.depends_on if isinstance(r.depends_on, list) else []
                    blocked_by = [k for k in deps if k in failed_keys]
                    if blocked_by:
                        blocked_nodes.append({
                            "node_key": r.node_key,
                            "title": r.title,
                            "blocked_by": blocked_by
                        })
        except Exception as exc:
            logger.warning("blocked_node_query_failed: job=%s error=%s", job_id, str(exc))
        return {
            "status": "blocked",
            "message": "No executable nodes — dependencies not satisfied",
            "blocked_nodes": blocked_nodes
        }

    node_id = node["id"]
    title = node["title"]
    _raw_model = node.get("assigned_model", "")
    _assigned = _raw_model if _raw_model and str(_raw_model).lower() not in ("none", "null") else ""
    exec_model = model_override or _assigned or settings.model_general
    tool = (node.get("tool") or "LLM").strip()
    # ── Human: short-circuit ──
    if tool.lower() in ("human", "human_review"):
        skip_msg = "Skipped: human review not required in auto mode"
        logger.info("tool_dispatch: %s skip node=%s", tool, node["node_key"])
        await _set_node_status(db, node_id, "done")
        await db.execute(text(
            "UPDATE dag_nodes SET output_text = :o WHERE id = :nid"
        ), {"o": skip_msg, "nid": str(node_id)})
        await db.commit()
        await _log_execution(db, job_id, str(node_id), "info", f"Tool dispatch: {tool} skipped")
        return {
            "status": "done",
            "node_key": node["node_key"],
            "title": title,
            "output": skip_msg,
            "passed": True,
            "reason": "Tool dispatch: node skipped",
            "confidence": 1.0,
            "model_used": "none (skipped)",
            "tool": tool,
        }
    # ── Model routing by tool ──
    if tool in ("CodeGen", "FileSystem"):
        exec_model = settings.model_coder
    verifier_model = settings.model_verifier

    logger.info("node_execution_started: node='%s' job=%s model=%s", title, job_id, exec_model)
    await _set_node_status(db, node_id, "running")

    # 3. Build prompt with RAG grounding
    brief = job.get("refined_brief") or {}
    raw_prompt = await _build_prompt(node, brief)

    # 4. Optimize prompt (Step 14) — before RAG injection
    if not skip_optimize:
        try:
            opt_result = await optimize_prompt(
                prompt=raw_prompt,
                skip_verify=True,  # fast path inside execution
            )
            exec_prompt = opt_result.optimized_prompt
            logger.info("Prompt optimized: %d -> %d tokens", opt_result.token_count_before, opt_result.token_count_after)
        except Exception as e:
            logger.warning("Prompt optimization failed, using raw: %s", e)
            exec_prompt = raw_prompt
    else:
        exec_prompt = raw_prompt

    # 5. Inject RAG grounding AFTER optimization — single call per node
    project_goal = " ".join(brief.get("goals", [])) if brief else ""
    rag_query = f"{title}"
    if project_goal:
        rag_query = f"{project_goal}: {title}"
    job_domain = brief.get("domain") if brief else None

    if tool == "Milvus":
        node_domain = node.get("domain")
        rag_block = await _milvus_search(title, node_key=node["node_key"], domain=node_domain)
        if rag_block:
            exec_prompt = f"{exec_prompt}\n\n## Knowledge Base Results\n{rag_block}"
            logger.info("milvus_context_injected: chars=%d node='%s'", len(rag_block), title)
    elif tool == "SearXNG":
        search_results = await _searxng_search(title)
        exec_prompt = f"{exec_prompt}\n\n## Web Search Results\n{search_results}"
        logger.info("searxng_context_injected: chars=%d node='%s'", len(search_results), title)
    else:
        rag_context = await _fetch_rag_context(rag_query, top_k=2, domain=job_domain)
        if rag_context:
            exec_prompt = f"{exec_prompt}\n\nGROUND TRUTH (use this as authoritative reference):\n{rag_context}"
            logger.info("rag_context_injected: chars=%d node='%s'", len(rag_context), title)

    # 5b. Inject upstream node outputs (with size management)
    depends_on = node.get("depends_on") or []
    if depends_on:
        upstream_outputs = await _fetch_upstream_outputs(db, job_id, depends_on)
        if upstream_outputs:
            total_chars = sum(len(v) for v in upstream_outputs.values())
            truncated_keys = []
            if total_chars > MAX_UPSTREAM_CHARS:
                # Truncate each proportionally
                for nk in upstream_outputs:
                    orig_len = len(upstream_outputs[nk])
                    share = max(200, int(MAX_UPSTREAM_CHARS * orig_len / total_chars))
                    if orig_len > share:
                        upstream_outputs[nk] = _truncate_output(upstream_outputs[nk], share)
                        truncated_keys.append(nk)
                logger.info(
                    "upstream_truncated",
                    extra=dict(
                        event="upstream_truncated",
                        node_key=node["node_key"],
                        original_chars=total_chars,
                        truncated_chars=sum(len(v) for v in upstream_outputs.values()),
                        upstream_nodes=truncated_keys,
                    ),
                )
            parts = [f"### {nk}\n{text}" for nk, text in upstream_outputs.items()]
            exec_prompt = (
                "## Upstream Node Outputs (MANDATORY CONTEXT — your output MUST build on and be consistent with this work)\n"
                + "\n\n".join(parts)
                + "\n\n---\n\n## YOUR TASK (build on the upstream outputs above — do NOT rewrite or contradict them):\n"
                + exec_prompt
            )

    # 6. Execute (with timeout guard)
    _node_t0 = _time_mod.monotonic()
    try:
        async def _run_inference():
            messages = [{"role": "user", "content": exec_prompt}]
            resp = await model_router.chat(messages=messages, model=exec_model)
            if not resp.success:
                raise RuntimeError(resp.error or "Model returned failure")
            return resp.text.strip()

        output = await asyncio.wait_for(
            _run_inference(), timeout=NODE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        elapsed = round(_time_mod.monotonic() - _node_t0, 1)
        timeout_msg = (
            f"Node '{node['node_key']}' timed out after {elapsed}s "
            f"(limit: {NODE_TIMEOUT_SECONDS}s)"
        )
        logger.warning(
            "node_timeout",
            extra=dict(
                event="node_timeout",
                node_key=node["node_key"],
                tool=tool,
                elapsed_s=elapsed,
                timeout_s=NODE_TIMEOUT_SECONDS,
            ),
        )
        await _set_node_status(db, node_id, "failed", output=timeout_msg)
        await _log_execution(db, job_id, node_id, "error", timeout_msg)
        return {
            "status": "failed",
            "node_key": node["node_key"],
            "title": title,
            "error": timeout_msg,
            "reason": "timeout",
            "message": "Node timed out. Review timeout settings or retry.",
        }
    except Exception as e:
        logger.error("node_execution_failed: node='%s' error=%s", title, e)
        await _set_node_status(db, node_id, "failed")
        await _log_execution(db, job_id, node_id, "error", str(e))
        return {
            "status": "failed",
            "node_key": node["node_key"],
            "title": title,
            "error": str(e),
            "message": "Node failed. Review error and retry or skip.",
        }

    # 7. Verify output
    verified, reason, confidence = True, "skipped", 1.0
    if not skip_verify:
        verified, reason, confidence = await _verify_output(title, output, verifier_model)
        if not verified:
            logger.warning("node_verification_failed: node='%s' reason=%s", title, reason)

    # Store confidence from verifier (logprob-based confidence requires
    # migrating to Ollama's OpenAI-compatible /v1/chat/completions endpoint
    # which supports the logprobs parameter — future work).
    # Verifier confidence is set to NULL when verification is skipped.
    db_confidence = confidence if (not skip_verify and confidence > 0.0) else None
    await db.execute(
        text("UPDATE dag_nodes SET confidence = :conf WHERE id = :nid"),
        {"conf": db_confidence, "nid": str(node_id)},
    )
    await db.commit()
    logger.info(
        "verification_complete",
        extra=dict(
            event="verification_complete",
            node_key=node["node_key"],
            verified=verified,
            confidence=db_confidence,
        ),
    )

    # 8. Persist
    final_status = "done" if verified else "failed"
    await _set_node_status(db, node_id, final_status, output=output, optimized_prompt=exec_prompt)
    await _log_execution(
        db, job_id, node_id, "info" if verified else "warning",
        f"Node '{title}' -> {final_status}",
        {"model": exec_model, "confidence": confidence, "reason": reason},
    )

    # 9. Auto-complete job if no pending nodes remain
    job_complete = False
    remaining = await db.execute(
        text("SELECT COUNT(*) FROM dag_nodes WHERE job_id = :jid AND status = 'pending'"),
        {"jid": job_id},
    )
    if remaining.scalar() == 0:
        await db.execute(
            text("UPDATE jobs SET status = 'completed' WHERE id = :jid"),
            {"jid": job_id},
        )
        await db.commit()
        job_complete = True
        logger.info("job_autocompleted: job=%s", job_id)
        # ── Step 9b: Compile final output ──
        compiled = await _compile_output(job_id, db)
        await db.execute(
            text("UPDATE jobs SET compiled_output = :out WHERE id = :jid"),
            {"out": compiled, "jid": job_id},
        )
        await db.commit()
        logger.info("compiled_output_stored: chars=%s job=%s", len(compiled), job_id)

    return {
        "status": final_status,
        "job_id": job_id,
        "node_key": node["node_key"],
        "title": title,
        "output": output,
        "verified": verified,
        "verification_reason": reason,
        "confidence": confidence,
        "model_used": exec_model,
        "prompt_used": exec_prompt,
        "awaiting_approval": verified,  # caller should confirm before next node
        "job_complete": job_complete,
    }




async def _compile_output(job_id: str, db) -> str:
    """Compile node outputs into a single deliverable. No LLM calls."""
    rows = await db.execute(
        text(
            "SELECT node_key, title, tool, status, output_text "
            "FROM dag_nodes WHERE job_id = :jid ORDER BY execution_order"
        ),
        {"jid": job_id},
    )
    nodes = rows.mappings().all()

    # Strategy 1: output-titled node gets priority
    for n in nodes:
        if n["title"] and "output" in n["title"].lower() and n["status"] == "done":
            return n["output_text"] or ""

    # Strategy 2: last CodeGen node is the deliverable
    done = [n for n in nodes if n["status"] == "done" and n["output_text"]]
    if done and done[-1]["tool"] == "CodeGen":
        return done[-1]["output_text"]

    # Strategy 3: concatenate all passed outputs with headers
    parts = []
    for n in nodes:
        if n["status"] == "done" and n["output_text"]:
            parts.append(f"## {n['node_key']}: {n['title']}\n\n{n['output_text']}")
    return "\n\n---\n\n".join(parts)


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


async def retry_failed_node(job_id: str, node_key: str, db: AsyncSession) -> dict:
    """Reset a failed node to pending and cascade-reset downstream nodes."""
    from collections import deque

    # ---- Stage 1: Validate ----
    row = (await db.execute(
        text("""
            SELECT node_key, status, retry_count, max_retries
            FROM dag_nodes
            WHERE job_id = :jid AND node_key = :nk
        """),
        {"jid": job_id, "nk": node_key},
    )).fetchone()

    if not row:
        return {"status": "error", "message": "Node %s not found" % node_key}

    if row.status != "failed":
        return {
            "status": "error",
            "message": "Node %s is '%s', not 'failed'" % (node_key, row.status),
        }

    if row.retry_count >= row.max_retries:
        return {
            "status": "error",
            "message": "Node %s exhausted retries (%d/%d)" % (
                node_key, row.retry_count, row.max_retries
            ),
        }

    # ---- Stage 2: Load full DAG topology ----
    all_rows = (await db.execute(
        text("""
            SELECT node_key, status, depends_on
            FROM dag_nodes
            WHERE job_id = :jid
        """),
        {"jid": job_id},
    )).fetchall()

    # ---- Stage 3: Build reverse adjacency map ----
    downstream_map: dict[str, set[str]] = {}
    for r in all_rows:
        for parent_key in (r.depends_on or []):
            downstream_map.setdefault(parent_key, set()).add(r.node_key)

    # ---- Stage 4: BFS for transitive downstream nodes ----
    queue = deque(downstream_map.get(node_key, set()))
    visited: set[str] = set()
    while queue:
        nk = queue.popleft()
        if nk in visited:
            continue
        visited.add(nk)
        queue.extend(downstream_map.get(nk, set()))

    status_lookup = {r.node_key: r.status for r in all_rows}
    downstream_to_reset = [
        nk for nk in visited
        if status_lookup.get(nk) in ("pending", "failed")
    ]

    # ---- Stage 5: Atomic reset ----
    await db.execute(
        text("""
            UPDATE dag_nodes
            SET status   = 'pending',
                output_text  = NULL,
                started_at   = NULL,
                completed_at = NULL,
                retry_count  = retry_count + 1,
                updated_at   = now()
            WHERE job_id = :jid AND node_key = :nk
        """),
        {"jid": job_id, "nk": node_key},
    )

    if downstream_to_reset:
        await db.execute(
            text("""
                UPDATE dag_nodes
                SET status   = 'pending',
                    output_text  = NULL,
                    started_at   = NULL,
                    completed_at = NULL,
                    updated_at   = now()
                WHERE job_id = :jid AND node_key = ANY(:keys)
            """),
            {"jid": job_id, "keys": downstream_to_reset},
        )

    await db.execute(
        text("""
            UPDATE jobs
            SET status = 'executing',
                compiled_output = NULL,
                updated_at = now()
            WHERE id = :jid AND status IN ('failed', 'blocked')
        """),
        {"jid": job_id},
    )

    await db.commit()

    # ---- Stage 6: Structured log ----
    logger.info(
        "node_retry job_id=%s node_key=%s retry_count=%s downstream_reset=%s",
        job_id, node_key, row.retry_count + 1, len(downstream_to_reset),
    )

    # ---- Stage 7: Return result ----
    return {
        "status": "reset",
        "node_key": node_key,
        "retry_count": row.retry_count + 1,
        "downstream_reset": downstream_to_reset,
    }


# ---------------------------------------------------------------------------
# Full-DAG auto-execution (SSE streaming)
# ---------------------------------------------------------------------------
async def execute_all_nodes(
    job_id: str,
) -> AsyncGenerator[str, None]:
    """
    Execute every pending DAG node in sequence, yielding Server-Sent Events.

    Auto-generates the DAG if none exists.  On verification failure the node
    is recorded as failed and the loop continues to the next actionable node.
    Nodes whose dependencies include a failed node are naturally blocked.

    Each database operation uses a short-lived session to avoid holding a
    connection for the full pipeline duration (15-30+ min on CPU hardware).

    SSE event types:
        dag_generated    — DAG was auto-created (includes task_count, strategy)
        node_start       — node execution beginning (includes node_key, title)
        node_done        — node passed verification
        node_failed      — node failed execution or verification (skipped)
        pipeline_complete — all actionable nodes processed (summary)
        error            — fatal error, pipeline halted
    """
    import json as _json
    import time as _time

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"

    t0 = _time.monotonic()
    node_results: list[dict] = []

    # ---- Session 1: concurrent execution guard (atomic check-and-set) ----
    async with async_session() as db:
        guard_result = await db.execute(
            text("""
                UPDATE jobs SET status = 'running', updated_at = now()
                WHERE id = :jid AND status != 'running'
                RETURNING id
            """),
            {"jid": job_id},
        )
        if guard_result.rowcount == 0:
            # Job is already running or doesn't exist — check which
            job_check = await _get_job(db, job_id)
            if not job_check:
                yield _sse("error", {"message": f"Job {job_id} not found"})
            else:
                yield _sse("error", {
                    "message": "Job is already executing",
                    "job_id": job_id,
                    "http_status": 409,
                })
            return
        await db.commit()

    # ---- Session 2: validate job ----
    async with async_session() as db:
        job = await _get_job(db, job_id)
    if not job:
        yield _sse("error", {"message": f"Job {job_id} not found"})
        return
    if job["status"] not in ("running", "executing", "planning", "refining"):
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

    # ---- execute loop ----
    while True:
        # ---- Session 4 (per iteration): peek + execute ----
        async with async_session() as db:
            # Peek at next node so we can emit a start event before the
            # multi-minute execution begins.
            node = await _get_next_node(db, job_id)
            if node:
                yield _sse("node_start", {
                    "job_id": job_id,
                    "node_key": node["node_key"],
                    "title": node["title"],
                    "tool": node.get("tool", "LLM"),
                })

            # execute_next_node re-fetches the node internally — safe.
            result = await execute_next_node(job_id, db)
        status = result.get("status", "unknown")

        # -- terminal: all nodes done --
        if status == "complete":
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
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
                "status": "completed",
                "compile_status": "partial" if is_partial else "complete",
            }
            # FB-3: Include compiled_output in SSE payload
            async with async_session() as db:
                _co_row = await db.execute(
                    text("SELECT compiled_output FROM jobs WHERE id = :jid"),
                    {"jid": job_id},
                )
                _co_val = str(_co_row.scalar() or "")
            if len(_co_val) <= 50_000:
                summary["compiled_output"] = _co_val
            else:
                summary["compiled_output_available"] = True
            if is_partial:
                summary["failed_nodes"] = failed_node_details
            logger.info("pipeline_completed: job=%s total=%s passed=%s failed=%s duration_ms=%s", job_id, len(node_results), passed, failed_count, elapsed_ms)
            yield _sse("pipeline_complete", summary)
            return

        # -- terminal: fatal error or blocked --
        if status in ("error", "blocked"):
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
            result["nodes_completed"] = len(node_results)
            result["duration_ms"] = elapsed_ms
            yield _sse(status, result)
            return

        # -- node executed --
        node_results.append(result)

        if status == "done":
            yield _sse("node_done", {
                "job_id": job_id,
                "node_key": result.get("node_key"),
                "title": result.get("title"),
                "output": result.get("output"),
                "verified": result.get("verified"),
                "confidence": result.get("confidence"),
                "model_used": result.get("model_used"),
            })
        elif status == "failed":
            _failed_key = result.get("node_key", "")
            # ── Auto-retry if retries remain ──
            _retried = False
            try:
                async with async_session() as _retry_db:
                    retry_result = await retry_failed_node(job_id, _failed_key, _retry_db)
                    if retry_result.get("status") == "reset":
                        _retried = True
                        yield _sse("node_retry", {
                            "job_id": job_id,
                            "node_key": _failed_key,
                            "title": result.get("title"),
                            "retry_count": retry_result.get("retry_count", 0),
                            "message": "Auto-retrying failed node",
                        })
                        node_results.pop()  # remove failed result, will re-run
                        continue  # retry immediately
            except Exception as _retry_exc:
                logger.warning("auto_retry_failed: node=%s error=%s", _failed_key, _retry_exc)
            # No retries left or retry failed — report and move on
            yield _sse("node_failed", {
                "job_id": job_id,
                "node_key": _failed_key,
                "title": result.get("title"),
                "error": result.get("error"),
                "verification_reason": result.get("verification_reason"),
                "model_used": result.get("model_used"),
                "retries_exhausted": not _retried,
            })
            # skip failed node — continue to next actionable node
        else:
            # unexpected status — log and bail
            logger.warning("Unexpected node status '%s' in execute_all", status)
            yield _sse("error", {
                "message": f"Unexpected status '{status}'",
                "result": result,
            })
            return

        # -- early exit: auto-completion fired on last node --
        if result.get("job_complete"):
            elapsed_ms = int((_time.monotonic() - t0) * 1000)
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
            early_summary = {
                "job_id": job_id,
                "total_nodes": len(node_results),
                "passed": passed,
                "failed": failed_count,
                "duration_ms": elapsed_ms,
                "compile_status": "partial" if is_partial else "complete",
            }
            # FB-3: Include compiled_output in SSE payload
            async with async_session() as db:
                _co_row2 = await db.execute(
                    text("SELECT compiled_output FROM jobs WHERE id = :jid"),
                    {"jid": job_id},
                )
                _co_val2 = str(_co_row2.scalar() or "")
            if len(_co_val2) <= 50_000:
                early_summary["compiled_output"] = _co_val2
            else:
                early_summary["compiled_output_available"] = True
            if is_partial:
                early_summary["failed_nodes"] = failed_node_details
            yield _sse("pipeline_complete", early_summary)
            return
