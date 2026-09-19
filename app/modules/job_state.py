"""§17.1107 — the ONE place a job's status changes.

Phase 1 ledger (2026-09-18) S-2..S-5: 37 raw ``UPDATE jobs SET status`` sites,
0 of them through a shared transition; ``fail_job`` (14 callers) and five
phase-success writes carried no status guard at all, so a late failure after an
operator cancel flipped ``cancelled → failed`` and a phase's success write
resurrected a cancelled job (``cancelled → planning|executing``). Four
divergent "terminal" tuples lived in four modules.

This module owns the vocabulary and the guarded write:

* ``JOB_STATUSES`` / ``TERMINAL_JOB_STATUSES`` / ``NODE_STATUSES`` — the real
  sets (``db/init.sql`` + migration 055). Node success is ``done``; nodes have
  no ``blocked``; no job ever starts at ``pending``.
* ``transition(db, job_id, to=..., ...)`` — one statement, one guard, one log
  line. The DEFAULT guard refuses to move a job that is already terminal; pass
  ``expected_from=`` for a tighter claim (``('running',)``) or
  ``allow_terminal=True`` for the few deliberate reopen paths. Extra columns
  ride in ``set_columns`` (whitelisted) so the status and its payload land
  atomically. Returns the PRIOR status on success, ``None`` when refused — the
  refusal is logged with the row's current status so a caller that ignores the
  return value still leaves evidence.

Migration policy: existing raw writes move here as they are touched;
``tests/test_job_state.py`` ratchets the raw-write count so it can only go
down. New status writes go through ``transition`` — no exceptions.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold.job_state")

JOB_STATUSES: frozenset[str] = frozenset({
    "pending", "refining", "awaiting_confirmation", "researching", "planning",
    "executing", "running", "completed", "failed", "cancelled", "blocked",
    "assisted_executing", "assisted_running", "assisted_paused",
    "aggregating", "awaiting_assist",
})

# A job in one of these never moves again on its own; only an explicit reopen
# (``allow_terminal=True``) may leave them. ``blocked`` is NOT terminal: retry,
# reopen and assist-start all leave it.
TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

# Real node vocabulary (``db/init.sql`` CHECK). Success is ``done``.
NODE_STATUSES: frozenset[str] = frozenset({"pending", "running", "done", "failed", "skipped"})

# Columns a transition may set alongside ``status`` — a whitelist, because the
# column name is interpolated into the statement.
SETTABLE_COLUMNS: frozenset[str] = frozenset({
    "error_summary", "refined_brief", "research_data", "workflow_summary",
    "dag_input_hash", "title", "input_text", "metadata", "compiled_output",
    "resume_attempts", "resume_done_marker", "completed_at",
})

ERROR_SUMMARY_MAX = 1000


def sql_status_list(statuses: Iterable[str], *, vocabulary: frozenset[str] = JOB_STATUSES) -> str:
    """Render ``'a', 'b'`` for an ``IN (...)`` clause, validating every name
    against the vocabulary so nothing but a known status literal is ever
    interpolated."""
    names = sorted({str(s) for s in statuses})
    if not names:
        raise ValueError("job_state: empty status list")
    bad = [s for s in names if s not in vocabulary]
    if bad:
        raise ValueError(f"job_state: unknown status literal(s) {bad!r}")
    return ", ".join(f"'{s}'" for s in names)


async def transition(
    db: AsyncSession,
    job_id: Any,
    *,
    to: str,
    expected_from: Iterable[str] | None = None,
    allow_terminal: bool = False,
    set_columns: Mapping[str, Any] | None = None,
    reason: str = "",
) -> str | None:
    """Move ``jobs.status`` to ``to`` in one guarded statement.

    Guard precedence: ``expected_from`` (``status IN (...)``) → else
    ``allow_terminal`` (no guard) → else the default ``status NOT IN
    (terminal)``. Returns the prior status when the row moved, ``None`` when
    the guard refused (logged at WARNING with the current status). Does NOT
    commit — the caller owns the transaction, as every existing site does.
    """
    if to not in JOB_STATUSES:
        raise ValueError(f"job_state.transition: unknown target status {to!r}")

    sets = ["status = :to", "updated_at = NOW()"]
    params: dict[str, Any] = {"id": str(job_id), "to": to}
    for col, val in (set_columns or {}).items():
        if col not in SETTABLE_COLUMNS:
            raise ValueError(f"job_state.transition: column {col!r} is not transition-settable")
        if col == "error_summary" and isinstance(val, str):
            val = val[:ERROR_SUMMARY_MAX]
        sets.append(f"{col} = :set_{col}")
        params[f"set_{col}"] = val

    if expected_from is not None:
        guard = f"jobs.status IN ({sql_status_list(expected_from)})"
    elif allow_terminal:
        guard = "TRUE"
    else:
        guard = f"jobs.status NOT IN ({sql_status_list(TERMINAL_JOB_STATUSES)})"

    # The FOR UPDATE subquery reads the row AFTER taking its lock, so the prior
    # status we return is the one the guard was evaluated against — not a
    # snapshot a concurrent writer may already have replaced.
    row = (await db.execute(
        text(
            "UPDATE jobs "
            f"SET {', '.join(sets)} "
            "FROM (SELECT id AS prior_id, status AS prior_status "
            "        FROM jobs WHERE id = :id FOR UPDATE) prior "
            f"WHERE jobs.id = prior.prior_id AND {guard} "
            "RETURNING prior.prior_status"
        ),
        params,
    )).first()
    if row is None:
        current = (await db.execute(
            text("SELECT status FROM jobs WHERE id = :id"), {"id": str(job_id)},
        )).scalar()
        logger.warning(
            "job_transition_refused: job=%s to=%s current=%s guard=%s reason=%s",
            job_id, to, current, guard, reason or "-",
        )
        return None
    prior = row[0]
    logger.info(
        "job_transition: job=%s from=%s to=%s reason=%s", job_id, prior, to, reason or "-",
    )
    return prior
