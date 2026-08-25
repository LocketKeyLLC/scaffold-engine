"""Research session lifecycle — DB-backed state machine, snapshots,
atomic claim for resume, heartbeat helpers, finalization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator

from sqlalchemy import text

from app.config import settings
from app.database import async_session

logger = logging.getLogger("scaffold.research.state")


def _ra():
    """Lazy lookup of the research_agent module so tests that patch
    ``app.modules.research_agent.X`` (e.g. ``async_session``,
    ``_finalize_session``) affect calls made from this module.

    The split on 2026-05-05 moved session helpers out of research_agent;
    tests still target the research_agent namespace, so we resolve the
    relevant dependencies through it at call-time.
    """
    import app.modules.research_agent as _m  # local to avoid import cycle at module load
    return _m


# =============================================================================
# Module constants
# =============================================================================

HEARTBEAT_INTERVAL_SECONDS = settings.research_heartbeat_interval

# Snapshot schema:
#   1 = legacy {title, content_hash} projection (pre-2026-04-22)
#   2 = full entries with content/source_url/confidence_score (current)
SNAPSHOT_SCHEMA_VERSION = 2


# =============================================================================
# ResearchState
# =============================================================================

@dataclass
class ResearchState:
    topic: str
    depth: str = "medium"
    domain: str = "eng"
    iteration: int = 0
    paused: bool = False
    search_history: set = field(default_factory=set)
    url_history: set = field(default_factory=set)
    all_entries: list = field(default_factory=list)
    # §17.812 (audit C5) — transient, per-iteration: set True when a SearXNG
    # query returns a non-200 (CAPTCHA / 429 / 403). The search loop reads it to
    # emit an SSE `warning` so a blocked backend surfaces instead of silently
    # yielding an empty/incomplete iteration. Not serialized (resets to False on
    # resume, and it's reset at the top of every iteration anyway).
    search_degraded: bool = False
    # §17.831 (plan 8.1) — run-level fetch tally, accumulated per iteration
    # from the `research_fetch` progress dict; surfaced on research_complete
    # as `fetch_stats` so a fetch outage (high failed, low ok) or a heavily
    # snippet-degraded run (high fallback_entries) is visible in the terminal
    # payload instead of only in server logs.
    fetch_attempted: int = 0
    fetch_ok: int = 0
    fetch_failed: int = 0
    fallback_entries: int = 0
    # §17.831 — why the summary fell back to the stub, or None when the real
    # summary was generated (`summary_timeout` / `summary_llm_failed` /
    # `summary_empty`). Pre-§17.831 timeout and dead-model were deliberately
    # indistinguishable; the plan (8.1) reversed that call.
    summary_fallback: str | None = None
    # §17.833 (plan 8.3 / audit M8) — the cite-aware summary's numbered source
    # list ({url, source_type, confidence_score}, index = [n]-1), stamped by
    # _generate_summary in cite mode so research_complete carries the EXACT
    # list the inline [n] markers refer to. None on the default path.
    cited_sources: list | None = None
    total_ingested: int = 0
    total_rejected: int = 0
    total_new: int = 0
    total_versioned: int = 0
    total_skipped_hash: int = 0
    outline_facets: list = field(default_factory=list)
    covered_facets: set = field(default_factory=set)
    gap_queries: list = field(default_factory=list)
    # §17.448 (Phase B / B1) — faithfulness score of the generated summary vs
    # the collected sources, stamped by _generate_summary when the check is
    # enabled. None = not scored. Surfaced on the research_complete payload.
    faithfulness: dict | None = None
    # §17.452 (Phase C) — CoVe revision metadata ({changed, questions}) stamped
    # by _generate_summary when the check is enabled. None = not run.
    cove: dict | None = None
    # §17.799 — per-citation ATTRIBUTION score of the (cite-aware) summary vs the
    # SPECIFIC source each inline [n] marker cites (app.modules.citation_faithfulness).
    # Stamped by _generate_summary when citation_faithfulness_check_enabled. Shape:
    # {score, supported, total, cited, dangling, unsupported_citations}. None = not
    # scored. Surfaced on the research_complete payload + summary block.
    citation_faithfulness: dict | None = None
    # §17.662 — user-tailored decision options ("branches") surfaced from the
    # research, stamped by _generate_summary when the topic is decision-shaped.
    # None = not run / not applicable (a straightforward factual topic gets NO
    # fabricated choices). Shape: {decision, options:[{label,fit,tradeoff}],
    # suggested, why}. Surfaced on the research_complete payload + summary block.
    options: dict | None = None
    # §17.811 — latest progress + ETA snapshot (ProgressTracker.snapshot()),
    # folded into state_snapshot so the read/reconnect surface shows an ETA.
    # None until the first iteration ticks. soft_total (early-exit on
    # convergence) → the ETA is an upper bound.
    progress: dict | None = None

    @property
    def max_iterations(self) -> int:
        # §17.549 — deeper gap-analysis coverage of more sub-topics.
        return {"shallow": 2, "medium": 3, "deep": 6}.get(self.depth, 3)


# =============================================================================
# SSE + heartbeat
# =============================================================================

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _touch_last_activity(session_id: str) -> None:
    """§17.167 — tickle ``research_sessions.last_activity_at`` after a
    forward-progress event (an LLM task in the heartbeat loop returned).

    Called ONLY after a task actually completes — never on the
    fixed-interval heartbeat tick. This preserves the §17.85 reaper's
    "no real progress = dead" semantics: a session wedged inside a
    single ``model_router.generate`` / ``tool_call`` await still ages
    out after ``stale_threshold_minutes`` because the wedged call
    never returns and the touch never fires. A slow-but-progressing
    session (e.g. topic-mode iteration with multiple LLM sub-steps,
    each taking 5-10 min on the CPU embedder, total >30 min) gets
    its activity stamp bumped after each sub-step and stays visible
    to the reaper as legitimately alive.

    Fail-soft — a transient DB hiccup must never break the SSE flow.
    """
    try:
        async with _ra().async_session() as db:
            await db.execute(
                text(
                    "UPDATE research_sessions SET last_activity_at = NOW() "
                    "WHERE id = :sid"
                ),
                {"sid": session_id},
            )
            await db.commit()
    except Exception as exc:
        logger.warning(
            "touch_last_activity_failed: session=%s error=%s",
            session_id, exc,
        )


async def _await_with_heartbeat(
    task: asyncio.Task,
    heartbeat_payload: dict,
    interval: int | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Yield heartbeat SSE while ``task`` runs. Caller reads ``task.result()`` after.

    Waits with ``asyncio.wait({task}, timeout=ivl)`` instead of unconditional
    sleep, so an instantly-completing task (e.g. AsyncMock in tests) adds
    zero latency. Previously slept a full interval per call regardless of
    task state, which compounded across iterations.

    §17.167 — when ``session_id`` is provided, touches
    ``last_activity_at`` on task completion so the §17.85 reaper sees
    sub-step forward progress within a long-running iteration. Optional
    parameter so the helper stays callable from non-session contexts
    (tests, future non-research uses).
    """
    ivl = interval or HEARTBEAT_INTERVAL_SECONDS
    while not task.done():
        done, _pending = await asyncio.wait({task}, timeout=ivl)
        if task.done():
            break
        yield _sse("heartbeat", heartbeat_payload)
    if session_id is not None:
        await _touch_last_activity(session_id)


# =============================================================================
# Session tracking
# =============================================================================

async def _guard_and_create_session(
    topic: str, depth: str, domain: str, owner: str | None = None,
) -> tuple[str | None, dict | None]:
    """Atomically create a 'running' research session.

    Relies on the unique partial index ``uq_research_sessions_single_running``
    (migration 020) to enforce the singleton-running invariant. Replaces the
    previous TOCTOU pair (``_guard_concurrent`` SELECT + ``_create_session``
    INSERT).

    Returns
    -------
    (session_id, None)
        Success — row inserted, caller owns the session.
    (None, {"id": ..., "topic": ...})
        Another session is already running. Caller should emit 409.
    (None, None)
        Insert raced with a concurrent finalize. Caller should emit 409.
    """
    from sqlalchemy.exc import IntegrityError

    insert_sql = text(
        "INSERT INTO research_sessions (topic, depth, domain, status, owner) "
        "VALUES (:topic, :depth, :domain, 'running', :owner) "
        "RETURNING id"
    )
    params = {"topic": topic, "depth": depth, "domain": domain, "owner": owner}

    async with _ra().async_session() as db:
        try:
            result = await db.execute(insert_sql, params)
            session_id = str(result.scalar_one())
            await db.commit()
            return session_id, None
        except IntegrityError:
            await db.rollback()

    async with _ra().async_session() as db:
        row = await db.execute(
            text("SELECT id, topic FROM research_sessions WHERE status = 'running' LIMIT 1")
        )
        existing = row.mappings().first()
        return None, (dict(existing) if existing else None)


def _build_snapshot(state: ResearchState) -> dict:
    """JSON-safe snapshot of ResearchState for persistence.

    Persists FULL entries (content, source, confidence_score, title,
    content_hash) so resume can regenerate a faithful summary. The legacy
    ``entries_projection`` field is retained for read-side back-compat only.
    """
    full_entries = []
    for e in state.all_entries:
        h = e.get("content_hash") or hashlib.sha256(
            (e.get("content") or "").encode("utf-8")
        ).hexdigest()[:16]
        full_entries.append({
            "title": e.get("title", ""),
            "content": e.get("content", ""),
            # §17.600 — every research producer stores the URL under "source"
            # (research_agent.py); the old "source_url" key always read null,
            # so resumed entries silently lost their URL and dropped out of the
            # Sources block. Serialize the real key.
            "source": e.get("source"),
            "confidence_score": e.get("confidence_score"),
            "content_hash": h,
        })

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "iteration": state.iteration,
        "search_history": sorted(state.search_history),
        "url_history": sorted(state.url_history),
        "entries": full_entries,
        "outline_facets": state.outline_facets,
        "covered_facets": sorted(state.covered_facets),
        "gap_queries": state.gap_queries,
        "totals": {
            "ingested": state.total_ingested,
            "rejected": state.total_rejected,
            "new": state.total_new,
            "versioned": state.total_versioned,
            "skipped_hash": state.total_skipped_hash,
        },
        # §17.811 — progress + ETA snapshot for the read/reconnect surface.
        "progress": state.progress,
    }


async def _update_session_iteration(
    session_id: str,
    state: ResearchState,
    coverage: float | None = None,
) -> None:
    snapshot = _build_snapshot(state)
    async with _ra().async_session() as db:
        await db.execute(
            text("""
                UPDATE research_sessions
                SET iterations_completed = :iters,
                    total_entries_extracted = :extracted,
                    total_entries_ingested = :ingested,
                    total_entries_rejected = :rejected,
                    total_urls_searched = :urls,
                    total_queries = :queries,
                    coverage_pct = COALESCE(:coverage, coverage_pct),
                    state_snapshot = CAST(:snapshot AS JSONB),
                    updated_at = NOW(),
                    last_activity_at = NOW()
                WHERE id = :sid
            """),
            {
                "sid": session_id,
                "iters": state.iteration,
                "extracted": len(state.all_entries),
                "ingested": state.total_ingested,
                "rejected": state.total_rejected,
                "urls": len(state.url_history),
                "queries": len(state.search_history),
                "coverage": coverage,
                "snapshot": json.dumps(snapshot),
            },
        )
        await db.commit()


async def _pause_session(
    session_id: str,
    state: ResearchState,
    question: str,
    ttl_seconds: int = 3600,
) -> None:
    snapshot = _build_snapshot(state)
    async with _ra().async_session() as db:
        await db.execute(
            text("""
                UPDATE research_sessions
                SET status = 'paused_awaiting_reply',
                    pause_question = :question,
                    pause_expires_at = NOW() + make_interval(secs => :ttl),
                    state_snapshot = CAST(:snapshot AS JSONB),
                    updated_at = NOW(),
                    last_activity_at = NOW()
                WHERE id = :sid
            """),
            {
                "sid": session_id,
                "question": question,
                "ttl": ttl_seconds,
                "snapshot": json.dumps(snapshot),
            },
        )
        await db.commit()


async def _load_session_for_resume(session_id: str) -> dict | None:
    async with _ra().async_session() as db:
        row = await db.execute(
            text("""
                SELECT id, topic, depth, domain, status, state_snapshot,
                       pause_question, pause_expires_at, pause_reply
                FROM research_sessions
                WHERE id = :sid
            """),
            {"sid": session_id},
        )
        r = row.mappings().first()
        return dict(r) if r else None


async def _atomic_claim_for_resume(session_id: str, reply: str) -> bool:
    """Atomic paused_awaiting_reply → running. Returns True if this caller won the race."""
    async with _ra().async_session() as db:
        result = await db.execute(
            text("""
                UPDATE research_sessions
                SET status = 'running',
                    pause_reply = :reply,
                    updated_at = NOW(),
                    last_activity_at = NOW()
                WHERE id = :sid
                  AND status = 'paused_awaiting_reply'
            """),
            {"sid": session_id, "reply": reply},
        )
        await db.commit()
        return result.rowcount == 1


def _rehydrate_state(row: dict) -> ResearchState:
    """Rehydrate ResearchState from snapshot JSON.

    Supports schema_version:
      - missing / 1 : legacy ``entries_projection`` (title + content_hash only)
      - 2           : full ``entries`` with content/source/confidence_score
    """
    snap = row.get("state_snapshot") or {}
    if isinstance(snap, str):
        snap = json.loads(snap) if snap else {}

    version = int(snap.get("schema_version", 1))

    state = ResearchState(
        topic=row["topic"],
        depth=row["depth"],
        domain=row["domain"],
    )
    state.iteration = int(snap.get("iteration", 0))
    state.search_history = set(snap.get("search_history", []))
    state.url_history = set(snap.get("url_history", []))
    state.outline_facets = list(snap.get("outline_facets", []))
    state.covered_facets = set(snap.get("covered_facets", []))
    state.gap_queries = list(snap.get("gap_queries", []))

    if version >= 2:
        state.all_entries = list(snap.get("entries", []))
        # §17.600 — pre-fix snapshots serialized the URL under "source_url"
        # (always null due to the key mismatch); normalize either shape to the
        # "source" key that consumers read.
        for _e in state.all_entries:
            if isinstance(_e, dict) and not _e.get("source") and _e.get("source_url"):
                _e["source"] = _e["source_url"]
    else:
        # v1 legacy: projection-only, lossy. Summary on resume will be degraded
        # but the session still completes rather than crashing on KeyError.
        state.all_entries = list(snap.get("entries_projection", []))
        logger.warning(
            "rehydrate_legacy_snapshot: session=%s schema_version=%s entries=%d",
            row.get("id"), version, len(state.all_entries),
        )

    totals = snap.get("totals", {})
    state.total_ingested = int(totals.get("ingested", 0))
    state.total_rejected = int(totals.get("rejected", 0))
    state.total_new = int(totals.get("new", 0))
    state.total_versioned = int(totals.get("versioned", 0))
    state.total_skipped_hash = int(totals.get("skipped_hash", 0))
    return state


async def _finalize_session(
    session_id: str,
    status: str,
    duration_ms: int,
    summary: str | None = None,
    error_message: str | None = None,
) -> None:
    async with _ra().async_session() as db:
        await db.execute(
            text("""
                UPDATE research_sessions
                SET status = :status,
                    completed_at = NOW(),
                    duration_ms = :dur,
                    summary = :summary,
                    error_message = COALESCE(:error_message, error_message),
                    updated_at = NOW()
                WHERE id = :sid
            """),
            {
                "sid": session_id,
                "status": status,
                "dur": duration_ms,
                "summary": summary,
                "error_message": error_message,
            },
        )
        await db.commit()


async def _run_with_session_lifecycle(
    session_id: str,
    coro_factory,
    t0: float,
    topic: str,
):
    """Unified cancellation-safe wrapper for all research entry points.

    Three disjoint exits:

    1. Inner flow completes normally
       The wrapped generator is responsible for calling ``_finalize_session``
       with its own terminal status (``completed`` / ``failed`` /
       ``paused_awaiting_reply``). We do nothing here.

    2. Generic ``Exception`` escapes the inner flow
       Finalize as ``failed`` with a typed error message, emit an SSE
       ``error`` event, and swallow. The caller sees a clean stream end.

    3. ``CancelledError`` or any other ``BaseException``
       Client disconnected (or outer task was cancelled). Finalize as
       ``cancelled`` with ``error_message='client_disconnect'`` BEFORE
       re-raising so asyncio semantics are preserved.

    The ``finalized`` flag guards against double-finalize when the inner
    flow partially progressed before raising.
    """
    finalized = False
    try:
        async for evt in coro_factory():
            yield evt
        finalized = True
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "research_entry_failed: session=%s error=%s",
            session_id, exc, exc_info=True,
        )
        try:
            await _ra()._finalize_session(
                session_id, "failed", elapsed_ms,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            logger.exception("finalize_failed_during_error_handler: session=%s", session_id)
        finalized = True
        yield _sse("error", {
            "message": f"Research failed: {exc}",
            "session_id": session_id,
            "topic": topic,
        })
    finally:
        if not finalized:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "research_cancelled: session=%s elapsed_ms=%d",
                session_id, elapsed_ms,
            )
            # §17.168 — wrap the finalize UPDATE in asyncio.shield so it
            # completes even when our surrounding generator is being
            # cancelled. Without shield, the cancellation propagated by
            # the SSE disconnect-watch wrapper bubbles into this await
            # and raises CancelledError before _finalize_session commits;
            # since CancelledError is a BaseException (not Exception)
            # the prior ``except Exception`` did not catch it, and the
            # session row stayed ``status='running'`` forever. Shield
            # lets the inner coroutine continue running on the event
            # loop independently of the caller's cancellation.
            try:
                await asyncio.shield(
                    _ra()._finalize_session(
                        session_id, "cancelled", elapsed_ms,
                        error_message="client_disconnect",
                    )
                )
            except asyncio.CancelledError:
                # Caller-side cancellation hit while waiting for the
                # shielded finalize. The DB UPDATE is still in flight
                # on the event loop and will commit. Log + re-raise so
                # the cancellation continues to propagate correctly.
                logger.warning(
                    "finalize_cancel_propagated_but_shielded: session=%s "
                    "— UPDATE continues on loop",
                    session_id,
                )
                raise
            except Exception:
                logger.exception(
                    "finalize_failed_during_cancel: session=%s", session_id,
                )
