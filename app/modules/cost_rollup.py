"""Sprint J.3.b — cost + latency rollup queries.

Two readers over `llm_call_logs`:

  - ``get_job_cost_totals(job_id, db)`` → flat dict with summed cost,
    tokens, latency, and call count. Cheap (single SUM query). Used
    by ``execution_status`` to surface a lightweight cost block on
    every ``/exec/status`` call.

  - ``get_job_costs(job_id, db)`` → totals + per-(provider, model)
    breakdown. Used by the dedicated ``/jobs/{id}/costs`` endpoint
    when an operator wants the detailed view.

Both fail open: a missing ``llm_call_logs`` table (test env without
the J.3 migration) or a transient DB error returns the zero-shape
rather than 500ing. Telemetry consumers should still get *a*
response, just with all-zero values.

§17.284 — every fail-open return carries ``data_source`` (``"ok"`` |
``"error"``). Pre-§17.284 a zero rollup was indistinguishable from a
transient DB failure that fell through to the zero shape — operators
reading ``/exec/status`` on a job with no calls yet saw the same dict
as on a job whose query just blew up. Callers can now distinguish:

  - ``data_source == "ok"``: query ran, the zeros (if any) are real.
  - ``data_source == "error"``: at least one component query raised;
    the shape is a fallback and the operator should re-poll or look at
    logs.

Composite responses (``get_job_costs``) downgrade to ``"error"`` if
ANY component query failed — never silently mix valid totals with a
failed breakdown without flagging it.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("scaffold.cost_rollup")


_TOTALS_SQL = """
    SELECT
        COALESCE(SUM(cost_usd), 0)              AS total_cost_usd,
        COALESCE(SUM(prompt_tokens), 0)         AS total_prompt_tokens,
        COALESCE(SUM(completion_tokens), 0)     AS total_completion_tokens,
        COALESCE(SUM(latency_ms), 0)            AS total_latency_ms,
        COUNT(*)                                AS call_count
    FROM llm_call_logs
    WHERE job_id = :jid
"""

_BREAKDOWN_SQL = """
    SELECT
        provider,
        model,
        COUNT(*)                                AS calls,
        COALESCE(SUM(cost_usd), 0)              AS cost_usd,
        COALESCE(SUM(prompt_tokens), 0)         AS prompt_tokens,
        COALESCE(SUM(completion_tokens), 0)     AS completion_tokens,
        COALESCE(SUM(latency_ms), 0)            AS latency_ms
    FROM llm_call_logs
    WHERE job_id = :jid
    GROUP BY provider, model
    ORDER BY cost_usd DESC, calls DESC
"""

# §17.90 — kind breakdown groups calls by their call_kind tag
# (currently only "synthesis"; everything else NULL → "uncategorized").
# COALESCE folds NULL into the literal string so the response shape is
# uniform; operators see a row for each meaningful bucket.
_KIND_BREAKDOWN_SQL = """
    SELECT
        COALESCE(call_kind, 'uncategorized')    AS kind,
        COUNT(*)                                AS calls,
        COALESCE(SUM(cost_usd), 0)              AS cost_usd,
        COALESCE(SUM(prompt_tokens), 0)         AS prompt_tokens,
        COALESCE(SUM(completion_tokens), 0)     AS completion_tokens,
        COALESCE(SUM(latency_ms), 0)            AS latency_ms
    FROM llm_call_logs
    WHERE job_id = :jid
    GROUP BY COALESCE(call_kind, 'uncategorized')
    ORDER BY cost_usd DESC, calls DESC
"""


def _zero_totals(*, data_source: str = "ok") -> dict[str, Any]:
    """Zero-shape totals dict. ``data_source`` distinguishes a real empty
    rollup (``"ok"``) from a fail-open fallback (``"error"``)."""
    return {
        "total_cost_usd": 0.0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_latency_ms": 0,
        "call_count": 0,
        "data_source": data_source,
    }


async def get_job_cost_totals(job_id: str, db) -> dict[str, Any]:
    """Return job-level cost/latency totals. Fail-open shape on error.

    Used by ``execution_handler.execution_status`` to surface a
    lightweight ``costs`` block on every ``/exec/status`` call. Single
    SUM query — cheap to add to a hot path.

    §17.284 — return dict carries ``data_source`` (``"ok"`` | ``"error"``).
    Real zero (no calls logged yet) is ``"ok"``; DB failure fallback is
    ``"error"`` so callers can tell the two apart.
    """
    try:
        row = await db.execute(text(_TOTALS_SQL), {"jid": str(job_id)})
        rec = row.mappings().first()
    except Exception as exc:
        logger.debug(
            "get_job_cost_totals_failed: job=%s error=%s "
            "(returning zero totals)", job_id, exc,
        )
        return _zero_totals(data_source="error")
    if rec is None:
        return _zero_totals(data_source="ok")
    return {
        "total_cost_usd": float(rec["total_cost_usd"] or 0.0),
        "total_prompt_tokens": int(rec["total_prompt_tokens"] or 0),
        "total_completion_tokens": int(rec["total_completion_tokens"] or 0),
        "total_latency_ms": int(rec["total_latency_ms"] or 0),
        "call_count": int(rec["call_count"] or 0),
        "data_source": "ok",
    }


async def get_job_costs(job_id: str, db) -> dict[str, Any]:
    """Return totals + per-(provider, model) breakdown for a job.

    Two SUM queries; called only by the dedicated
    ``/jobs/{id}/costs`` endpoint. Fail-open: returns the zero shape
    with an empty breakdown on any DB error so the endpoint can
    still serve a 200 (operator sees "no calls logged" rather than
    a 500).
    """
    totals = await get_job_cost_totals(job_id, db)
    # §17.284 — track per-query failure so the composite response can
    # report its data_source honestly: if ANY of the three queries fell
    # through to a fail-open shape, the outer dict is marked "error".
    breakdown_ok = True
    kind_ok = True

    try:
        rows = await db.execute(text(_BREAKDOWN_SQL), {"jid": str(job_id)})
        records = rows.mappings().all()
    except Exception as exc:
        logger.debug(
            "get_job_costs_breakdown_failed: job=%s error=%s "
            "(returning empty breakdown)", job_id, exc,
        )
        records = []
        breakdown_ok = False

    # §17.90 — kind breakdown is a separate fail-open query so a missing
    # column (pre-migration test env) or transient DB error returns an
    # empty list rather than 500ing the by_provider path too.
    try:
        kind_rows = await db.execute(
            text(_KIND_BREAKDOWN_SQL), {"jid": str(job_id)},
        )
        kind_records = kind_rows.mappings().all()
    except Exception as exc:
        logger.debug(
            "get_job_costs_kind_breakdown_failed: job=%s error=%s "
            "(returning empty kind breakdown)", job_id, exc,
        )
        kind_records = []
        kind_ok = False

    by_provider = [
        {
            "provider": r["provider"],
            "model": r["model"],
            "calls": int(r["calls"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "latency_ms": int(r["latency_ms"] or 0),
        }
        for r in records
    ]
    by_kind = [
        {
            "kind": r["kind"],
            "calls": int(r["calls"] or 0),
            "cost_usd": float(r["cost_usd"] or 0.0),
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "latency_ms": int(r["latency_ms"] or 0),
        }
        for r in kind_records
    ]
    # §17.284 — composite data_source: "error" if ANY component query
    # raised. ``totals`` already carries its own data_source from
    # ``get_job_cost_totals``; we OR it with the per-breakdown flags
    # here. The inner ``totals["data_source"]`` is overwritten by the
    # composite value via the ``**totals`` spread + this trailing key.
    totals_ok = totals.get("data_source", "ok") == "ok"
    composite_source = (
        "ok" if (totals_ok and breakdown_ok and kind_ok) else "error"
    )
    return {
        "job_id": str(job_id),
        **totals,
        "by_provider": by_provider,
        "by_kind": by_kind,
        "data_source": composite_source,
    }
