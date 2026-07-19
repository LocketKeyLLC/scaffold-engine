"""§17.297 — single source of truth for pipeline status icons.

§17.280-🟢-2 closeout. Pre-§17.297 the ``STATUS_ICONS`` dict was
inlined in all five pipeline files (``scaffold_router.py``,
``execution_handler.py``, ``dag_viewer.py``, ``gt_browser.py``,
``prompt_inspector.py``) with a "keep in sync" comment per the §17.212
constraint that OWUI's auto-discovery loads each ``pipelines/*.py`` as
an isolated module (no shared imports between pipeline files).

The ``_vendor/`` subdirectory IS invisible to discovery (underscore-
prefixed dir, non-recursive scan), so a single shared module here
collapses the 5-way replication. Adding or renaming a status now
requires editing this file ONLY — every pipeline sees the change next
deploy.

**Union policy.** ``STATUS_ICONS`` is the UNION of node-level and
job-level status keys. Most pipelines only look up node-level keys
(``done``, ``failed``, ``running``, ``pending``, ``skipped``);
``execution_handler.py`` is the one that needs the job-level extras
(``executing``, ``planning``, ``blocked``, ``completed``,
``cancelled``). Having extra entries in the dict is harmless — the
non-execution-handler pipelines simply never look them up.

**Pattern to add a status:** add the key here. That's it. No call-site
changes; no per-pipeline sync. The pre-§17.297 "─── SHARED: keep in
sync ───" comments are gone from each pipeline because they're no
longer load-bearing.
"""
from __future__ import annotations


# §17.297 — closed-set, append-only as new statuses are added to the
# JobStatus / NodeStatus enums in app/schemas.py. The keys mirror those
# enums; pipelines look up by the literal status string the orchestrator
# emits, so a new enum value needs a matching key here.
STATUS_ICONS: dict[str, str] = {
    # Node-level statuses (used by all 5 pipelines).
    "done":      "✅",
    "failed":    "❌",
    "running":   "🔄",
    "pending":   "⬜",
    "skipped":   "⏭️",
    # Job-level statuses (used by execution_handler.py).
    "executing": "🔄",
    "planning":  "📋",
    "blocked":   "🚫",
    "completed": "✅",
    "cancelled": "🚫",
    # §17.624 — hands-on job parked as a plan; needs the operator via /assist.
    "awaiting_assist": "🙋",
}
