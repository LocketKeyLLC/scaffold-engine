"""Per-job-status recovery actions registry (audit item 10).

The orchestrator's `jobs.status` column already encodes a 14-state lifecycle
state machine. This module exposes that state machine as a structured
"what to do next" registry. Both `/exec/status/{job_id}` and any CLI/SDK
caller can consult it to render concrete next-steps to a user without
duplicating the lookup logic.

This replaces the previous pattern where each consumer (OWUI pipeline
``_handle_results``, CLI ``scaffold jobs status``, etc.) hardcoded its
own list of recovery hints. The single registry below is the source of
truth.
"""
from __future__ import annotations

import logging
from typing import Any

from app.schemas import JobStatus

logger = logging.getLogger("scaffold.recovery")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# Each entry is a list of action descriptors. The descriptor shape:
#
#     {
#         "action": str,         # short kind tag (wait, confirm, retry, skip, …)
#         "command": str | None, # chat-form command with placeholders, or None
#         "endpoint": str | None,# REST equivalent for SDK/CLI callers
#         "method": str | None,  # HTTP method paired with `endpoint`
#         "description": str,    # human-readable rationale
#         "node_specific": bool, # True if `command`/`endpoint` references a
#                                # specific failed/blocked node_key — caller
#                                # must supply that context before rendering
#     }
#
# Placeholders use ``{job_id}`` and ``{node_key}``; the helper below fills
# them in. Statuses with no actionable next step (e.g. ``cancelled``)
# return a single "delete" or "nothing" action.

NEXT_ACTIONS: dict[str, list[dict[str, Any]]] = {
    "pending": [
        {
            "action": "wait",
            "command": None,
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Job is queued for refinement; status will advance shortly.",
            "node_specific": False,
        },
    ],
    "refining": [
        {
            "action": "wait",
            "command": None,
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Idea refinement in progress; check /results when done.",
            "node_specific": False,
        },
    ],
    "awaiting_confirmation": [
        {
            "action": "confirm",
            "command": "/confirm {job_id}",
            "endpoint": "/ideate/confirm",
            "method": "POST",
            "description": "Approve and proceed to research, planning, and execution.",
            "node_specific": False,
        },
        {
            "action": "delete",
            "command": None,
            "endpoint": "/jobs/{job_id}",
            "method": "DELETE",
            "description": "Abandon the job and remove it.",
            "node_specific": False,
        },
    ],
    "researching": [
        {
            "action": "wait",
            "command": None,
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Research + ingest in progress (typically 10–25 min on CPU).",
            "node_specific": False,
        },
    ],
    "planning": [
        {
            "action": "wait",
            "command": None,
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "DAG generation in progress.",
            "node_specific": False,
        },
    ],
    # §17.529 — umbrella (task decomposition) parent: alive while its component
    # children run. It has no DAG of its own; /exec/status returns the child
    # rollup. Finalizes to completed/failed when all children are terminal.
    "aggregating": [
        {
            "action": "wait",
            "command": "/results {job_id}",
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Component jobs are running; check the rollup with /results.",
            "node_specific": False,
        },
    ],
    # §17.624 — the hands-on assist gate parked this job as a plan (predominantly
    # Shell/human DAG); nodes are pending and the operator drives execution.
    "awaiting_assist": [
        {
            "action": "start_assist",
            "command": "/assist {job_id}",
            "endpoint": "/assist/start",
            "method": "POST",
            "description": "This is a hands-on plan on real systems — step through "
                           "it yourself with the engine guiding and verifying each step.",
            "node_specific": False,
        },
        {
            "action": "view_plan",
            "command": "/results {job_id}",
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Review the generated plan before starting.",
            "node_specific": False,
        },
    ],
    "executing": [
        {
            "action": "wait",
            "command": None,
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Execution in progress; SSE available at POST /execute/all.",
            "node_specific": False,
        },
        {
            "action": "skip_node",
            "command": "/skip {job_id} {node_key}",
            "endpoint": "/skip",
            "method": "POST",
            "description": "Mark a stuck node as skipped to unblock downstream work.",
            "node_specific": True,
        },
    ],
    "running": [
        {
            "action": "wait",
            "command": None,
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Execution in progress; SSE available at POST /execute/all.",
            "node_specific": False,
        },
        {
            "action": "skip_node",
            "command": "/skip {job_id} {node_key}",
            "endpoint": "/skip",
            "method": "POST",
            "description": "Mark a stuck node as skipped to unblock downstream work.",
            "node_specific": True,
        },
    ],
    "completed": [
        {
            "action": "view_output",
            "command": "/results {job_id}",
            "endpoint": "/exec/status/{job_id}",
            "method": "GET",
            "description": "Compiled output is available; use /results to render it.",
            "node_specific": False,
        },
    ],
    "failed": [
        {
            "action": "retry_node",
            "command": "/exec retry {job_id} {node_key}",
            "endpoint": "/exec/retry",
            "method": "POST",
            "description": "Reset a failed node to pending and resume execution.",
            "node_specific": True,
        },
        {
            "action": "skip_node",
            "command": "/skip {job_id} {node_key}",
            "endpoint": "/skip",
            "method": "POST",
            "description": "Abandon a failed node and unblock downstream work.",
            "node_specific": True,
        },
        {
            "action": "delete",
            "command": None,
            "endpoint": "/jobs/{job_id}",
            "method": "DELETE",
            "description": "Give up on the job and remove it.",
            "node_specific": False,
        },
    ],
    "blocked": [
        {
            "action": "retry_node",
            "command": "/exec retry {job_id} {node_key}",
            "endpoint": "/exec/retry",
            "method": "POST",
            "description": "Reset a blocked node to pending; downstream resumes once it passes.",
            "node_specific": True,
        },
        {
            "action": "skip_node",
            "command": "/skip {job_id} {node_key}",
            "endpoint": "/skip",
            "method": "POST",
            "description": "Skip the blocking node to unblock downstream nodes.",
            "node_specific": True,
        },
    ],
    "cancelled": [
        {
            "action": "rerun",
            "command": "/idea <re-state the original idea>",
            "endpoint": "/ideate",
            "method": "POST",
            "description": "Resubmit the idea — there's no in-place restart, but a fresh /ideate is ~30s and reuses any KB entries from the prior run.",
            "node_specific": False,
        },
        {
            "action": "delete",
            "command": None,
            "endpoint": "/jobs/{job_id}",
            "method": "DELETE",
            "description": "Job is cancelled; remove it if no longer needed.",
            "node_specific": False,
        },
    ],
    "assisted_executing": [
        {
            "action": "next_step",
            "command": "/assist next {session_id}",
            "endpoint": "/assist/{session_id}/next",
            "method": "GET",
            "description": "Claim the next step in the human-driven walk.",
            "node_specific": False,
        },
        {
            "action": "pause",
            "command": "/assist pause {session_id}",
            "endpoint": "/assist/{session_id}/pause",
            "method": "POST",
            "description": "Pause the assist session.",
            "node_specific": False,
        },
    ],
    "assisted_running": [
        {
            "action": "next_step",
            "command": "/assist next {session_id}",
            "endpoint": "/assist/{session_id}/next",
            "method": "GET",
            "description": "Claim the next step.",
            "node_specific": False,
        },
        {
            "action": "submit",
            "command": "/assist submit {session_id} {node_key}",
            "endpoint": "/assist/{session_id}/submit",
            "method": "POST",
            "description": "Record human-supplied evidence for the current node.",
            "node_specific": True,
        },
    ],
    "assisted_paused": [
        {
            "action": "resume",
            "command": "/assist resume {session_id}",
            "endpoint": "/assist/{session_id}/resume",
            "method": "POST",
            "description": "Resume the paused assist session.",
            "node_specific": False,
        },
        {
            "action": "abandon",
            "command": "/assist done {session_id}",
            "endpoint": "/assist/{session_id}",
            "method": "DELETE",
            "description": "Close the assist session and cancel the job.",
            "node_specific": False,
        },
    ],
}


# ---------------------------------------------------------------------------
# Reaper-driven outcomes (§17.134)
# ---------------------------------------------------------------------------
#
# Reaper-killed jobs land in ``failed`` or ``cancelled`` with one of a small
# set of error_summary strings — see ``app/modules/cleanup.py``. The base
# NEXT_ACTIONS registry treats those statuses generically; this layer lets
# us prepend more-specific guidance (e.g. "use POST /jobs/{id}/resume"
# instead of "rerun /idea from scratch") when the error_summary matches a
# known reaper pattern.
#
# Substring matching keeps the patterns robust to the dynamic-N strings the
# reaper emits ("Job timed out after 30 minutes of inactivity" varies with
# the configured threshold). Order doesn't matter — each pattern's prefix
# is unambiguous.

_REAPER_REASON_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Awaiting confirmation gate timeout", "reaper_awaiting_confirmation"),
    ("Stale planning state",                "reaper_planning_stale"),
    ("Assist session abandoned",            "reaper_assist_abandoned"),
    ("Long-phase job timed out",            "reaper_long_phase_timeout"),
    ("Research session timed out",          "reaper_research_session_timeout"),
    ("Pause expired before user reply",     "reaper_paused_research_expired"),
    ("Job timed out after",                 "reaper_execution_timeout"),
    ("client_disconnect",                   "phase2_client_disconnect"),
    ("crash_resume_budget_exhausted",       "crash_resume_budget"),
)


def classify_error_summary(error_summary: str | None) -> str | None:
    """Map a job's ``error_summary`` to a ``reason_kind`` tag, or None.

    Recognized patterns mirror the strings emitted by
    ``app/modules/cleanup.py`` (each reap-stage SQL block sets a distinct
    error_summary). Unknown summaries — including user-supplied ones
    from explicit failure paths — return None so the caller falls back
    to the generic NEXT_ACTIONS entries for the job's status.
    """
    if not error_summary:
        return None
    for substring, kind in _REAPER_REASON_PATTERNS:
        if substring in error_summary:
            return kind
    return None


# Reason-specific actions prepended to the base NEXT_ACTIONS list when
# classify_error_summary returns a hit. Each entry carries a `reason_kind`
# annotation so renderers can flag "killed by reaper" without re-running
# the classifier.

REAPER_REASON_ACTIONS: dict[str, list[dict[str, Any]]] = {
    # Reaped from awaiting_confirmation timeout — the refined brief is
    # intact but the user never sent /confirm. Resume picks up at DAG
    # generation; execute_all_nodes auto-generates the DAG.
    "reaper_awaiting_confirmation": [
        {
            "action": "resume",
            "command": None,
            "endpoint": "/jobs/{job_id}/resume",
            "method": "POST",
            "description": (
                "Confirmation timed out before you replied. Resume re-uses the "
                "refined brief and runs the rest of the pipeline."
            ),
            "node_specific": False,
        },
    ],
    # Planning sat past threshold without progress. Same recovery as
    # awaiting_confirmation: resume into execute_all_nodes which will
    # auto-generate the DAG.
    "reaper_planning_stale": [
        {
            "action": "resume",
            "command": None,
            "endpoint": "/jobs/{job_id}/resume",
            "method": "POST",
            "description": (
                "Planning stalled past threshold and was cancelled. Resume "
                "re-runs DAG generation + execution."
            ),
            "node_specific": False,
        },
    ],
    # Assist session abandoned (idle > assist_idle_threshold_days). The
    # owning job is `cancelled`; the assist_sessions row is `abandoned`.
    # Resume would skip Assist Mode — usually not what the user wants,
    # so the hint is to start fresh.
    "reaper_assist_abandoned": [
        {
            "action": "restart_assist",
            "command": "/idea <re-state the original idea>",
            "endpoint": "/ideate",
            "method": "POST",
            "description": (
                "Assist session abandoned past idle threshold. Start a fresh "
                "/ideate, then /assist start on the new job."
            ),
            "node_specific": False,
        },
    ],
    # Job was running/executing and the reaper killed it. The stuck
    # node is the right target for /exec/retry; downstream cascades
    # back to pending automatically.
    "reaper_execution_timeout": [
        {
            "action": "retry_node",
            "command": "/exec retry {job_id} {node_key}",
            "endpoint": "/exec/retry",
            "method": "POST",
            "description": (
                "Reaper killed a node stuck past the inactivity threshold. "
                "Retry the offending node — done nodes are preserved."
            ),
            "node_specific": True,
        },
    ],
    # Long-phase reap — researching / refining / planning past
    # long_phase_stale_minutes. Job is `failed`; if a refined_brief
    # exists, /jobs/resume is wrong (status != cancelled). User can
    # delete and /ideate again.
    "reaper_long_phase_timeout": [
        {
            "action": "rerun",
            "command": "/idea <re-state the original idea>",
            "endpoint": "/ideate",
            "method": "POST",
            "description": (
                "Long-phase activity timed out. Start a fresh /ideate — "
                "any ingested KB entries from the prior run are re-usable."
            ),
            "node_specific": False,
        },
    ],
    # Research session reaped (separate from the parent job's status).
    # The research_session row is failed; the job carries no remediation
    # action of its own. Hint user toward the research surface.
    "reaper_research_session_timeout": [
        {
            "action": "restart_research",
            "command": "/research <topic>",
            "endpoint": "/research",
            "method": "POST",
            "description": (
                "Research session timed out before completion. Re-fire "
                "/research — any partially-ingested entries are reusable."
            ),
            "node_specific": False,
        },
    ],
    # Pause-resume timed out. User missed the window to /research/reply.
    "reaper_paused_research_expired": [
        {
            "action": "restart_research",
            "command": "/research <topic>",
            "endpoint": "/research",
            "method": "POST",
            "description": (
                "Pause expired before you replied. Re-fire /research from "
                "the start — the prior session is unrecoverable."
            ),
            "node_specific": False,
        },
    ],
    # §17.774 — crash-resume gave up: the job was orphaned by a process crash
    # and relaunched at startup, but repeated restarts made no new progress
    # (a node likely keeps killing the process). Job sits in `failed`; the
    # offending node is the retry target — done nodes are preserved.
    "crash_resume_budget": [
        {
            "action": "retry_node",
            "command": "/exec retry {job_id} {node_key}",
            "endpoint": "/exec/retry",
            "method": "POST",
            "description": (
                "Auto-resume gave up after repeated crashes with no progress — "
                "a node likely keeps killing the process. Inspect it, then retry "
                "the offending node (completed nodes are preserved)."
            ),
            "node_specific": True,
        },
    ],
    # Phase 2 client disconnect (Round 7 fix). Job sits in `failed` with
    # the legacy `client_disconnect` summary. User can re-/confirm.
    "phase2_client_disconnect": [
        {
            "action": "reconfirm",
            "command": "/confirm {job_id}",
            "endpoint": "/ideate/confirm",
            "method": "POST",
            "description": (
                "Phase 2 was cut short by a client disconnect. Re-fire /confirm "
                "to resume research + planning."
            ),
            "node_specific": False,
        },
    ],
}


def next_actions_for(
    status: str,
    job_id: str,
    *,
    failed_node_key: str | None = None,
    blocked_node_key: str | None = None,
    running_node_key: str | None = None,
    error_summary: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve the registry into concrete actions for one job's current state.

    Placeholders are substituted with the supplied identifiers. For
    node-specific actions, the caller picks which node to point at:
    ``failed_node_key`` for ``failed``/``blocked`` retries, ``running_node_key``
    for in-flight skip suggestions, etc. When no node context is
    available, node-specific entries are returned with ``{node_key}``
    left in place — the caller can render the placeholder verbatim.

    ``error_summary`` (§17.134): when supplied and matching a known
    reaper / failure pattern (see ``classify_error_summary``), the
    matching entries from ``REAPER_REASON_ACTIONS`` are PREPENDED to
    the base status actions. Each prepended entry carries a
    ``reason_kind`` field so renderers can flag "killed by reaper" or
    "client disconnect" without re-running the classifier. The base
    actions still follow, so generic remediation (delete, retry-node,
    skip) remains discoverable as a fallback.

    Returns ``[]`` for unknown status values; logs a warning so an
    out-of-band status surfaces in monitoring.
    """
    template = NEXT_ACTIONS.get(status)
    if template is None:
        logger.warning("recovery_unknown_status: status=%s", status)
        return []

    # Pick the most informative node_key for substitution; preference
    # order matches typical recovery flows (failed nodes are the
    # primary target for retry/skip; blocked next, then running).
    node_key = failed_node_key or blocked_node_key or running_node_key or "{node_key}"

    # §17.134 — classify error_summary and prepend reason-specific actions.
    prepend: list[dict[str, Any]] = []
    reason_kind = classify_error_summary(error_summary)
    if reason_kind is not None:
        reason_template = REAPER_REASON_ACTIONS.get(reason_kind, [])
        for entry in reason_template:
            action = dict(entry)
            action["reason_kind"] = reason_kind
            prepend.append(action)

    # §17.599 — the assisted_* actions use {session_id} in their /assist
    # command + endpoint templates, but assist_sessions.id != jobs.id, so
    # filling session_id with job_id produced 404-ing links. Use the real
    # session_id when the caller supplies it; fall back to job_id only for
    # legacy callers that don't (no assisted_* action is reachable without a
    # session, so the fallback never renders a wrong assist link in practice).
    sid = session_id or job_id
    resolved: list[dict[str, Any]] = []
    for entry in (*prepend, *template):
        action = dict(entry)  # copy so we don't mutate the registry
        if action.get("command"):
            action["command"] = action["command"].format(
                job_id=job_id, node_key=node_key, session_id=sid,
            )
        if action.get("endpoint"):
            action["endpoint"] = action["endpoint"].format(
                job_id=job_id, node_key=node_key, session_id=sid,
            )
        resolved.append(action)
    return resolved


def all_known_statuses() -> tuple[str, ...]:
    """Return every status value the registry covers — used by tests to
    assert parity against ``JobStatus``."""
    return tuple(NEXT_ACTIONS.keys())
