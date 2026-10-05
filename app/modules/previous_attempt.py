"""§17.1366 — a step that failed before is drafted with what failed and why.

ADD132 drafted **seven** times on 2026-10-04, and the seventh rediscovered
ground the second had already covered. The run on attempt six did most of the
work — `qBittorrent login: Ok.`, the credentials written into `[Preferences]`
and verified on the machine — and then failed loudly (`--fail-with-body`, exit
22) on Radarr's API:

    'Config Contract' must not be empty.
    'Priority' must be between 1 and 50. You entered 0.

The engine then diagnosed it, correctly and in detail: *"it sent an incomplete
payload … the correct move is to update the existing client … sends the full
existing client object back with PUT, so configContract and priority stay
intact."*

And the next draft was made blind to all of it. From `llm_traces`:

    23:24:45   51,271 chars   "Config Contract" present   <- diagnose_failure
    23:25:21   51,271 chars   "Config Contract" present   <- diagnose_failure
    23:25:46   10,834 chars   absent                      <- the DRAFTER
    23:27:40   17,106 chars   absent                      <- the drafter again

The redraft added `priority: 1` — half of one error, carried by luck through the
operator-facing text — and still omitted `configContract`.

`POST /nodes/{job}/{key}/reset` is the documented way to give a supervised step
another attempt (§17.1284), and it NULLs `output_text` and
`last_verification_reason`. It audits the pre-image first, so the whole report
survives in `dag_node_edits.before` — 11,465 bytes of it for ADD132, containing
the error and the diagnosis — read by nobody.

So the engine produced the knowledge, stored it, and threw it away at the moment
it was needed: the same shape as §17.1363 (a reading whose scope was recorded and
never consumed), one layer up.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold")

#: the sections worth carrying: what actually ran and what the engine concluded.
#: NOT "👉 Do this next", which is the operator-facing paste and the bulk of the
#: report.
_WANTED = ("Executed on", "What went wrong", "Diagnosis", "Fix")
#: per-section cap, so a retry's context does not crowd out the step itself
_SECTION_CHARS = 1400
_TOTAL_CHARS = 3600


def _sections(report: str) -> list:
    """``[(heading, body)]`` for the `## …` sections of a run report."""
    out: list = []
    parts = re.split(r"^##\s+(.+?)\s*$", str(report or ""), flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        out.append((parts[i].strip(), parts[i + 1].strip()))
    return out


def what_failed(report: str) -> str:
    """The decisive part of a previous attempt's report, for the drafter."""
    kept: list = []
    for head, body in _sections(report):
        if not any(w.lower() in head.lower() for w in _WANTED):
            continue
        if not body:
            continue
        kept.append(f"### {head}\n{body[:_SECTION_CHARS]}")
    if not kept:
        return ""
    return "\n\n".join(kept)[:_TOTAL_CHARS]


async def previous_attempt(db, job_id: str, node_key: str) -> str:
    """What this step produced last time, or ``""``.

    The node's own `output_text` when it is still there (a `failed` step keeps
    it), else the newest reset pre-image — `reset` NULLs the column but audits
    the old value first, so the report survives a rerun.
    """
    try:
        row = (await db.execute(
            text("SELECT output_text FROM dag_nodes WHERE job_id = :j AND node_key = :k"),
            {"j": job_id, "k": node_key})).scalar()
        if str(row or "").strip():
            return str(row)
        pre = (await db.execute(
            text("SELECT before->>'output_text' FROM dag_node_edits "
                 " WHERE job_id = :j AND node_key = :k AND op = 'reset' "
                 "   AND length(coalesce(before->>'output_text','')) > 200 "
                 " ORDER BY created_at DESC LIMIT 1"),
            {"j": job_id, "k": node_key})).scalar()
        return str(pre or "")
    except Exception as exc:
        logger.warning("previous_attempt_failed job=%s node=%s err=%r", job_id, node_key, exc)
        return ""


async def carry_forward(db, job_id: str, node_key: str, reading=None) -> str:
    """The block the drafter gets about this step's last attempt, or ``""``."""
    body = what_failed(await previous_attempt(db, job_id, node_key))
    if not body:
        return ""
    if reading is not None:
        reading.note(f"{node_key}'s previous attempt",
                     f"{len(body)} characters of what ran, what failed and the engine's diagnosis")
    return ("THIS STEP HAS BEEN TRIED BEFORE AND FAILED. What ran, what the machine answered, and the "
            "engine's own reading of it are below. Do not repeat the attempt that failed: the draft you "
            "write now must differ in the way the diagnosis says, and anything the last attempt got "
            "RIGHT (a value written, a service restarted, a login proved) is already done and must not "
            "be undone.\n\n" + body)
