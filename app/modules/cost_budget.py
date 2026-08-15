"""§17.777 — hard per-job cost/token budget enforcement.

The read side of the budget feature. Sprint J.3 already does the *tally*:
every ``model_router`` call lands in ``llm_call_logs`` (tokens + USD cost,
tagged by job via ContextVars) and ``cost_rollup.get_job_cost_totals`` sums
it. This module turns that running tally into an *enforced* cap.

Enforcement is at the **node boundary**, not mid-call. ``execute_next_node``
calls :func:`enforce_job_budget` in its Phase-1 session (before it claims /
builds / runs the next node); once the accumulated spend from already-executed
nodes exceeds a cap, the next node never starts and the job is hard-stopped
(``status='failed'``, ``error_summary='cost_budget_exhausted'``). Node-boundary
granularity is deliberate: streamed generations don't expose usable mid-stream
token counts (§17.776 note in config), and a partly-generated node would be
wasted work anyway — the clean stopping point is between nodes.

Two limits, checked independently (breaching **either** stops the job):

  - **tokens**: prompt + completion, summed across every call. Bites on the
    default all-Ollama deployment (local/:cloud tags are unpriced → $0).
  - **cost_usd**: only bites when the job's models are priced in
    ``model_costs`` (OpenAI / Anthropic, or seeded :cloud tags).

Effective cap per axis = per-job override (``jobs.token_budget`` /
``jobs.cost_budget_usd``) when non-NULL, else the settings default
(``cost_budget_default_max_tokens`` / ``cost_budget_default_max_usd``).
``0`` on an axis = unlimited for that axis.

**Fail-open everywhere.** The whole thing is gated behind the default-OFF
master valve ``settings.cost_budget_enforcement_enabled``. Even with it on,
a telemetry read error (totals ``data_source == 'error'``) or a missing job
row returns "not exceeded" — a flaky cost query must never kill a live job.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import text

from app.config import settings
from app.modules.cost_rollup import get_job_cost_totals

logger = logging.getLogger("scaffold.cost_budget")

# error_summary sentinel written to a job hard-stopped by a budget breach.
# Mirrors §17.774's CRASH_RESUME_BUDGET_SUMMARY convention so operators (and
# the /exec surface) get a stable, greppable terminal reason.
COST_BUDGET_SUMMARY = "cost_budget_exhausted"


@dataclass
class BudgetStatus:
    """Resolved caps + current spend for one job.

    ``max_tokens`` / ``max_cost_usd`` are the *effective* caps after folding
    the per-job override over the settings default; ``0`` means unlimited on
    that axis. ``*_remaining`` is ``None`` on an unlimited axis, else
    ``max(0, cap - spent)``. ``exceeded`` is True iff a bounded axis has been
    reached or passed. ``limit`` names the first axis that tripped
    (``"tokens"`` | ``"cost"`` | ``None``). ``data_source`` propagates the
    rollup's ``"ok"``/``"error"`` flag so callers can tell a real zero from a
    fail-open read.
    """

    enforcement_enabled: bool
    max_tokens: int
    max_cost_usd: float
    spent_tokens: int
    spent_cost_usd: float
    tokens_remaining: Optional[int]
    cost_remaining_usd: Optional[float]
    exceeded: bool
    limit: Optional[str]
    data_source: str


def resolve_job_budget(
    token_budget: Optional[int],
    cost_budget_usd: Optional[float],
) -> tuple[int, float]:
    """Fold the per-job override over the settings default.

    ``None`` override → inherit the settings default. A non-NULL override
    (including an explicit ``0`` = unlimited) wins over the default. Returns
    ``(max_tokens, max_cost_usd)`` where ``0`` on an axis = unlimited.
    """
    max_tokens = (
        int(token_budget)
        if token_budget is not None
        else int(settings.cost_budget_default_max_tokens or 0)
    )
    max_cost_usd = (
        float(cost_budget_usd)
        if cost_budget_usd is not None
        else float(settings.cost_budget_default_max_usd or 0.0)
    )
    return max(0, max_tokens), max(0.0, max_cost_usd)


async def _load_job_budget(db, job_id: str) -> Optional[tuple[Optional[int], Optional[float]]]:
    """Return the raw ``(token_budget, cost_budget_usd)`` override for a job.

    ``None`` (job row missing) is distinct from ``(None, None)`` (row present,
    no overrides set → inherit defaults). Fail-open: a query error (e.g. the
    columns don't exist in a pre-062 test DB) returns ``(None, None)`` so the
    caller falls back to the settings defaults rather than raising.
    """
    try:
        row = await db.execute(
            text("SELECT token_budget, cost_budget_usd FROM jobs WHERE id = :jid"),
            {"jid": str(job_id)},
        )
        rec = row.mappings().first()
    except Exception as exc:
        logger.debug("load_job_budget_failed: job=%s error=%s", job_id, exc)
        return (None, None)
    if rec is None:
        return None
    tb = rec["token_budget"]
    cb = rec["cost_budget_usd"]
    return (
        int(tb) if tb is not None else None,
        float(cb) if cb is not None else None,
    )


async def get_budget_status(job_id: str, db) -> BudgetStatus:
    """Read-only: resolve caps + current spend for a job. Never raises.

    Used by the ``/jobs/{id}/costs`` surface to show budget + remaining, and
    internally by :func:`enforce_job_budget`. Independent of the master valve
    (it always reports the numbers); ``enforcement_enabled`` echoes the valve
    so callers can decide whether the ``exceeded`` flag should *act*.
    """
    override = await _load_job_budget(db, job_id)
    tb, cb = (None, None) if override is None else override
    max_tokens, max_cost_usd = resolve_job_budget(tb, cb)

    totals = await get_job_cost_totals(job_id, db)
    spent_tokens = int(totals["total_prompt_tokens"]) + int(totals["total_completion_tokens"])
    spent_cost = float(totals["total_cost_usd"])
    data_source = totals.get("data_source", "ok")

    tokens_remaining = max(0, max_tokens - spent_tokens) if max_tokens > 0 else None
    cost_remaining = max(0.0, max_cost_usd - spent_cost) if max_cost_usd > 0 else None

    # A fail-open telemetry read must never *report* a breach — the spend
    # numbers behind it are untrustworthy. exceeded stays False on 'error'.
    limit: Optional[str] = None
    if data_source == "ok":
        if max_tokens > 0 and spent_tokens >= max_tokens:
            limit = "tokens"
        elif max_cost_usd > 0 and spent_cost >= max_cost_usd:
            limit = "cost"

    return BudgetStatus(
        enforcement_enabled=bool(settings.cost_budget_enforcement_enabled),
        max_tokens=max_tokens,
        max_cost_usd=max_cost_usd,
        spent_tokens=spent_tokens,
        spent_cost_usd=spent_cost,
        tokens_remaining=tokens_remaining,
        cost_remaining_usd=cost_remaining,
        exceeded=limit is not None,
        limit=limit,
        data_source=data_source,
    )


def status_to_dict(status: BudgetStatus) -> dict[str, Any]:
    """Flatten a :class:`BudgetStatus` for JSON responses / SSE payloads."""
    return {
        "enforcement_enabled": status.enforcement_enabled,
        "max_tokens": status.max_tokens,
        "max_cost_usd": status.max_cost_usd,
        "spent_tokens": status.spent_tokens,
        "spent_cost_usd": status.spent_cost_usd,
        "tokens_remaining": status.tokens_remaining,
        "cost_remaining_usd": status.cost_remaining_usd,
        "exceeded": status.exceeded,
        "limit": status.limit,
    }


async def enforce_job_budget(db, job_id: str) -> Optional[dict[str, Any]]:
    """Hard-stop ``job_id`` if it has exceeded its budget. Node-boundary gate.

    Returns ``None`` when the job may proceed (valve off, no cap set, under
    budget, or a fail-open read). When a bounded axis is at/over its cap it
    flips the job to ``'failed'`` with ``error_summary=COST_BUDGET_SUMMARY``
    (guarded on the job still being in a live status so a racing writer wins)
    and returns a terminal dict the executor turns into a ``budget_exhausted``
    SSE frame and an early return.

    Runs on the caller's Phase-1 session and commits its own write so the
    terminal status is durable even though the caller returns immediately
    after. Never raises — enforcement must not become a new failure mode.
    """
    if not settings.cost_budget_enforcement_enabled:
        return None
    try:
        status = await get_budget_status(job_id, db)
    except Exception as exc:  # defensive: get_budget_status already fails open
        logger.warning("enforce_job_budget_read_failed: job=%s error=%s", job_id, exc)
        return None

    if not status.exceeded:
        return None

    try:
        flipped = (await db.execute(
            text(
                """
                UPDATE jobs
                   SET status = 'failed',
                       error_summary = :summary,
                       updated_at = NOW()
                 WHERE id = :jid
                   AND status IN ('running', 'executing', 'planning')
                RETURNING id
                """
            ),
            {"jid": str(job_id), "summary": COST_BUDGET_SUMMARY},
        )).first()
        await db.commit()
    except Exception as exc:
        logger.warning("enforce_job_budget_write_failed: job=%s error=%s", job_id, exc)
        # Still return the terminal dict — the caller must stop spending even
        # if we couldn't persist the status; the reaper / next call will catch
        # the durable state.
        flipped = None

    if status.limit == "tokens":
        msg = (
            f"Token budget exhausted: {status.spent_tokens} tokens spent "
            f"(cap {status.max_tokens}). Job stopped before the next node."
        )
    else:
        msg = (
            f"Cost budget exhausted: ${status.spent_cost_usd:.4f} spent "
            f"(cap ${status.max_cost_usd:.4f}). Job stopped before the next node."
        )
    logger.warning(
        "cost_budget_exhausted: job=%s limit=%s spent_tokens=%d spent_usd=%.4f "
        "cap_tokens=%d cap_usd=%.4f flipped=%s",
        job_id, status.limit, status.spent_tokens, status.spent_cost_usd,
        status.max_tokens, status.max_cost_usd, flipped is not None,
    )
    return {
        "status": "budget_exhausted",
        "job_id": str(job_id),
        "reason": COST_BUDGET_SUMMARY,
        "limit": status.limit,
        "message": msg,
        **status_to_dict(status),
    }
