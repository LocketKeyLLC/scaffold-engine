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


def next_actions_for(
    status: str,
    job_id: str,
    *,
    failed_node_key: str | None = None,
    blocked_node_key: str | None = None,
    running_node_key: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve the registry into concrete actions for one job's current state.

    Placeholders are substituted with the supplied identifiers. For
    node-specific actions, the caller picks which node to point at:
    ``failed_node_key`` for ``failed``/``blocked`` retries, ``running_node_key``
    for in-flight skip suggestions, etc. When no node context is
    available, node-specific entries are returned with ``{node_key}``
    left in place — the caller can render the placeholder verbatim.

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

    resolved: list[dict[str, Any]] = []
    for entry in template:
        action = dict(entry)  # copy so we don't mutate the registry
        if action.get("command"):
            action["command"] = action["command"].format(
                job_id=job_id, node_key=node_key, session_id=job_id,
            )
        if action.get("endpoint"):
            action["endpoint"] = action["endpoint"].format(
                job_id=job_id, node_key=node_key, session_id=job_id,
            )
        resolved.append(action)
    return resolved


def all_known_statuses() -> tuple[str, ...]:
    """Return every status value the registry covers — used by tests to
    assert parity against ``JobStatus``."""
    return tuple(NEXT_ACTIONS.keys())
