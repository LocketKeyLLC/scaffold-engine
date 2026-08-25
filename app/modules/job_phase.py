"""§17.820 (plan 5.9) — user-facing phase labels for job statuses.

Relocated verbatim from ``app/web/routes.py`` during the /web retirement:
``GET /work`` (app/routers/status.py) imports ``phase_label_for``, so the
mapping outlives the server-rendered UI that first grew it (§17.457/561).
"""
from __future__ import annotations

# The user-facing pipeline collapses the internal 9-state machine into the six
# phases people actually care about. Each entry maps a phase label to the raw
# job statuses that live under it.
PIPELINE_STEPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Refine", frozenset({"pending", "refining"})),
    ("Review", frozenset({"awaiting_confirmation"})),
    ("Research", frozenset({"researching"})),
    ("Plan", frozenset({"planning"})),
    ("Execute", frozenset({"executing", "running"})),
    ("Done", frozenset({"completed"})),
)

# Single user-facing phase label for ANY job status — backs the /work and
# /here "you-are-here" surfaces (§17.561). Covers the statuses the 6-phase
# grouping doesn't (umbrella aggregating, assist*, terminal-error).
_PHASE_LABEL_EXTRA: dict[str, str] = {
    "aggregating": "Assemble",
    "assisted_executing": "Execute",
    "assisted_running": "Execute",
    "assisted_paused": "Paused",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "blocked": "Blocked",
}


def phase_label_for(status: str) -> str:
    """Return the single user-facing phase label for a job status.

    Reuses the PIPELINE_STEPS groupings (Refine/Review/Research/Plan/
    Execute/Done) and falls back to _PHASE_LABEL_EXTRA for statuses outside
    the linear stepper. Unknown statuses degrade to a title-cased form.
    """
    for label, statuses in PIPELINE_STEPS:
        if status in statuses:
            return label
    return _PHASE_LABEL_EXTRA.get(status, status.replace("_", " ").title())
