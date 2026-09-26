"""Sprint J.3.a — cost + latency telemetry foundation.

Two responsibilities live in this module:

1. **ContextVars for job/node association.** Every `model_router.{generate,
   chat, embed, classify, tool_call}` call lands in `record_llm_call`, which
   stamps the resulting `llm_call_logs` row with whatever job/node context
   was active. Callers that own a long-running job (`execute_next_node`,
   `refine_idea`, `_run_with_session_lifecycle`) set the ContextVars on
   entry; off-job calls (`validate_models`, standalone `/optimize`) leave
   them unset and the row gets a NULL job_id — still tracked, just
   ungrouped.

2. **`record_llm_call(resp)`** — async fire-and-forget hook. Reads the
   ContextVars, computes cost via the `model_costs` table (Ollama / unknown
   provider falls through to 0), and INSERTs an `llm_call_logs` row. Wrapped
   in try/except so telemetry never breaks the call path. The DB write
   uses a **dedicated short-lived session** so it can't conflict with the
   caller's session-lifetime policy (e.g. ``execute_next_node`` holds NO
   session open across LLM calls; this helper opens its own).

The module-private ``_telemetry_enabled`` flag flips off in tests when the
``llm_call_logs`` table isn't present (e.g. test environments that skip
the migration); production paths run with telemetry on by default.
"""
from __future__ import annotations

import logging
import functools
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold.cost_tracking")

# ContextVars carry job/node identity across the async boundary into
# `record_llm_call` without requiring every model_router call site to
# thread them as kwargs. See the module docstring for which entry
# points set these.
current_job_id: ContextVar[Optional[str]] = ContextVar(
    "scaffold_current_job_id", default=None,
)
current_node_id: ContextVar[Optional[str]] = ContextVar(
    "scaffold_current_node_id", default=None,
)
# §17.90 — call_kind categorizes the LLM call for the cost rollup
# (e.g. "synthesis" for the W.7 compile-time polish pass). NULL by
# default; callers wrap their LLM call in ``call_kind("synthesis")``
# to tag it. Other categories may follow but each must be explicit —
# leaving it unset is the right answer for the generic execution path.
current_call_kind: ContextVar[Optional[str]] = ContextVar(
    "scaffold_current_call_kind", default=None,
)


@contextmanager
def call_kind(kind: str) -> Iterator[None]:
    """Tag every LLM call inside the ``with`` block with ``kind``.

    Uses ContextVar.set/reset so nested scopes (and concurrent asyncio
    tasks under the same event loop) don't leak across each other —
    same semantics as the existing ``current_job_id`` / ``current_node_id``
    contract. Designed for short-lived wrapping; for long-running setters
    (e.g. an entry-point that lives for an entire job), use
    ``current_call_kind.set(...)`` directly without resetting.
    """
    token = current_call_kind.set(kind)
    try:
        yield
    finally:
        current_call_kind.reset(token)


def tagged_calls(kind: str):
    """Tag every LLM call made inside an async function with ``kind``.

    §17.1179 (audit M17/M18) — `call_kind` had exactly ONE call site in the
    codebase (`execution_compile`'s "synthesis"); everything else fell through
    to `turn_timing.default_call_kind()`, which slugifies the OPERATOR-FACING
    STATUS LINE. Measured on this database: 34.7 hours of inference across
    19,171 calls carried `call_kind IS NULL` (every background pass, because
    they run outside a turn timer), and the 668 calls that were labelled split
    one call site across 30 buckets — `assist:preparing_the_walkthrough_for_t6`
    and `…_for_add49` are the same code. So the cost of the six-pass submit
    fan-out (M17) and the ~15-call state check (M18) could not be attributed at
    all, and nothing could be bounded that could not first be measured.

    A decorator rather than eight `with` blocks at eight call sites: the tag
    belongs to the pass, and sibling call sites drift.
    """
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with call_kind(kind):
                return await fn(*args, **kwargs)
        wrapper.__scaffold_call_kind__ = kind
        return wrapper
    return decorate


async def _lookup_rate(db, provider: str, model: str) -> tuple[float, float]:
    """Return ``(input_per_1m_usd, output_per_1m_usd)`` for the given
    provider/model. Missing row → (0, 0) so unknown providers and local
    Ollama models surface as zero cost without seed updates."""
    if not provider or not model:
        return 0.0, 0.0
    try:
        row = await db.execute(
            text(
                "SELECT input_per_1m_usd, output_per_1m_usd "
                "FROM model_costs "
                "WHERE provider = :p AND model = :m"
            ),
            {"p": provider, "m": model},
        )
        rec = row.first()
    except Exception:
        return 0.0, 0.0
    if rec is None:
        return 0.0, 0.0
    return float(rec[0] or 0.0), float(rec[1] or 0.0)


async def compute_cost_usd(
    db,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Compute total USD cost for one LLM call.

    Formula: ``(prompt_tokens × input_rate + completion_tokens × output_rate) / 1_000_000``.
    Missing rate row → 0. Negative or zero token counts → 0 (defensive
    against partial provider responses).
    """
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return 0.0
    in_rate, out_rate = await _lookup_rate(db, provider, model)
    if in_rate == 0.0 and out_rate == 0.0:
        return 0.0
    pt = max(0, prompt_tokens)
    ct = max(0, completion_tokens)
    return (pt * in_rate + ct * out_rate) / 1_000_000.0


async def record_llm_call(resp) -> None:
    """Insert one ``llm_call_logs`` row for a completed LLM call.

    ``resp`` is a ``ModelResponse``-shaped object (carries provider,
    model, tokens_prompt, tokens_completion, total_duration_ms, success).
    Runs in its own short-lived async session so it can't deadlock
    against a caller that's already holding a session open.

    Failures swallowed at WARNING — telemetry must never break the
    LLM call path. If the ``llm_call_logs`` table is missing (test env
    without the J.3 migration), the warning fires once and subsequent
    calls silently skip.
    """
    try:
        # Defer the import so module-level loading order doesn't matter
        # (model_router is imported very early; cost_tracking sits below
        # the model_router boundary). Same pattern as `app.modules`
        # callers that lazy-import `app.model_router`.
        from app.database import async_session
    except Exception:
        return  # database wiring unavailable — silently skip

    provider = (getattr(resp, "provider", "") or "").strip()
    model = (getattr(resp, "model", "") or "").strip()
    prompt_tokens = int(getattr(resp, "tokens_prompt", 0) or 0)
    completion_tokens = int(getattr(resp, "tokens_completion", 0) or 0)
    latency_ms = int(getattr(resp, "total_duration_ms", 0) or 0)
    success = bool(getattr(resp, "success", False))
    # §17.1139 (ledger L-7) — keep WHY it failed: the provider's/driver's text,
    # bounded. A zero-latency `success=false` row was unclassifiable before.
    error = None if success else (str(getattr(resp, "error", "") or "")[:300] or None)

    job_id = current_job_id.get()
    node_id = current_node_id.get()
    kind = current_call_kind.get()
    # §17.1109 (ledger L-1) — attribute the call to the active assist turn's
    # stage: accrue latency to it, and when the caller set no call_kind (every
    # assist stage, until now) default to ``assist:<stage>`` so the call log
    # says which stage paid. Fail-soft like everything else here.
    try:
        from app.utils import turn_timing
        turn_timing.note_llm_call(latency_ms)
        if kind is None:
            kind = turn_timing.default_call_kind()
    except Exception:
        pass

    try:
        async with async_session() as db:
            cost = await compute_cost_usd(
                db, provider, model, prompt_tokens, completion_tokens,
            )
            await db.execute(
                text(
                    "INSERT INTO llm_call_logs ("
                    "  job_id, node_id, provider, model, "
                    "  prompt_tokens, completion_tokens, latency_ms, "
                    "  cost_usd, success, call_kind, error"
                    ") VALUES ("
                    "  :job_id, :node_id, :provider, :model, "
                    "  :prompt_tokens, :completion_tokens, :latency_ms, "
                    "  :cost_usd, :success, :call_kind, :error"
                    ")"
                ),
                {
                    "job_id": job_id,
                    "node_id": node_id,
                    "provider": provider or "unknown",
                    "model": model or "unknown",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "latency_ms": latency_ms,
                    "cost_usd": cost,
                    "success": success,
                    "call_kind": kind,
                    "error": error,
                },
            )
            await db.commit()
    except Exception as exc:
        # Cost-tracking failures must never break the LLM call path.
        # Warning so silent breakage shows up in logs but doesn't bubble.
        logger.warning(
            "record_llm_call_failed: provider=%s model=%s error=%s",
            provider, model, exc,
        )

    # Sprint X.26 — Prometheus mirror. Runs after the DB insert so a
    # failed insert still produces a metric (the operator sees the
    # "DB sink down" symptom in the metric stream too).
    try:
        from app.observability import metrics as _metrics
        _metrics.record_llm_call(
            provider=provider, model=model,
            success=success, latency_ms=latency_ms,
        )
    except Exception:
        logger.debug("record_llm_call_metrics_failed", exc_info=True)
