"""§17.190 — Shared SSE event-name constants.

The orchestrator emits Server-Sent Events from three module families
(execution_agent / assist_agent / research_agent) and the OWUI pipeline
side (``pipelines/scaffold_router.py``) matches event-type strings to
render the right UI. Pre-§17.190 both sides used string literals,
and there was no shared module ensuring they agreed — a rename on either
side silently broke rendering.

This module is the single source of truth for the event-name vocabulary.
The vendored copy at ``pipelines/_vendor/_sse_events.py`` keeps the same byte-equal
constants accessible from the OWUI side (which doesn't import ``app.*``
at runtime). ``make sync-sse-events`` refreshes the vendor; ``make
check-sse-events`` is the CI gate (parallel to §17.186's schemas-in-sync
mechanism).

**Real drift surfaced by this refactor.** The pre-§17.190 audit found
``pipelines/scaffold_router.py`` L1646-1648 matching ``"node_started"`` /
``"node_completed"`` — neither of which is ever emitted (the orchestrator
emits ``node_start`` / ``node_done``). Those branches were dead code; the
assist UI lost node-progress rendering during the post-handoff autonomous
run. §17.190 fixes the consumer to use the canonical names.

The vocabulary is split into source-grouped namespaces so the relationship
between emitter and event name stays legible. ``ALL_EVENT_NAMES`` is the
frozenset of every defined value; ``test_sse_event_inventory.py`` scans
both emitter and consumer files for event-name strings and asserts every
hit is present in this set.
"""

# ---------------------------------------------------------------------------
# Execution-agent events (app/modules/execution_agent.py)
# ---------------------------------------------------------------------------
# Lifecycle for each DAG node during /execute/all and the post-assist-
# handoff autonomous run.

NODE_START = "node_start"
NODE_DONE = "node_done"
NODE_RETRY = "node_retry"
NODE_FAILED = "node_failed"
# §17.776 — per-node content delta during generation (valve
# node_token_streaming_enabled, default off). One NODE_TOKEN per stream_chat
# chunk carrying {job_id, node_key, delta}; the terminal output still arrives
# in NODE_DONE, so a consumer that ignores NODE_TOKEN renders unchanged.
NODE_TOKEN = "node_token"


# ---------------------------------------------------------------------------
# Assist-agent events (app/modules/assist_agent.py)
# ---------------------------------------------------------------------------
# Emitted when a /assist session hands off to the autonomous executor for
# a given node (started) and when that node completes (done).

ASSIST_HANDOFF_STARTED = "assist_handoff_started"
ASSIST_HANDOFF_DONE = "assist_handoff_done"
# §17.594 — single-mode handoff emits this when the target node is no longer
# 'pending' (already ran / not claimable), so nothing is handed to the
# autonomous executor. Informational; the consumer has no render branch.
ASSIST_HANDOFF_NOOP = "assist_handoff_noop"

# §17.493 — streamed walkthrough generation: one ASSIST_GUIDE_DELTA per content
# chunk, then a single ASSIST_GUIDE_DONE carrying the final status +
# guidance_meta (destructive scan, research sources, cached flag).
ASSIST_GUIDE_DELTA = "assist_guide_delta"
ASSIST_GUIDE_DONE = "assist_guide_done"

# §17.868 — the server-side turn loop (POST /assist/{sid}/message): ONE stream
# owns capture → deterministic gates → decide → dispatch → claim/premise →
# guidance, with a status frame at every stage. Born from a night of client-
# composed chains dying silently at their seams (§17.861–867): the client now
# renders events; it never sequences the loop.
ASSIST_TURN_STATUS = "assist_turn_status"      # {text} — progress line
ASSIST_TURN_ROUTED = "assist_turn_routed"      # {action, override|null}
ASSIST_NOTE_RECORDED = "assist_note_recorded"  # {kind, retracted, has_proposal}
ASSIST_REPLAN_PROPOSAL = "assist_replan_proposal"  # {proposal}
ASSIST_ANSWER = "assist_answer"                # {kind: ask|fix, text}
ASSIST_STEP_OUTCOME = "assist_step_outcome"    # {node_key, status}
ASSIST_TURN_DONE = "assist_turn_done"          # {handled} — terminal frame
# §17.869 — detached turn runs: the loop runs as a background task writing
# frames to assist_turn_runs; clients tail the row and RESUME after reload.
ASSIST_TURN_STARTED = "assist_turn_started"    # {run_id} — first frame of a tail


# ---------------------------------------------------------------------------
# Research-agent events (app/modules/research_agent.py)
# ---------------------------------------------------------------------------
# Phase-2 research lifecycle. Topic / URL / GitHub / PDF / OpenAPI modes
# share the same vocabulary; the per-mode runner emits a subset.

RESEARCH_STARTED = "research_started"
RESEARCH_RESUMED = "research_resumed"
RESEARCH_COMPLETE = "research_complete"
SEARCH_COMPLETE = "search_complete"
# §17.831 (plan 8.1) — live fetch/extract fan-out progress ({fetched, total,
# ok, failed, failed_reasons, last_url} per movement). The name existed in
# docs and log lines for months before this event was actually emitted.
RESEARCH_FETCH = "research_fetch"
EXTRACTION_COMPLETE = "extraction_complete"
INGESTION_COMPLETE = "ingestion_complete"
DECOMPOSITION_COMPLETE = "decomposition_complete"
ITERATION_STARTED = "iteration_started"
ITERATION_COMPLETE = "iteration_complete"
CONVERGENCE = "convergence"
GAP_ANALYSIS = "gap_analysis"
CACHE_HIT_UPSTREAM = "cache_hit_upstream"
SOURCE_REF_RESOLVED = "source_ref_resolved"
DISTILL_BYPASSED = "distill_bypassed"
CONTENT_TRUNCATED = "content_truncated"
EXTRACTOR_FALLBACK = "extractor_fallback"
QUALITY_GATE_FILTERED = "quality_gate_filtered"
CONTRADICTIONS_DETECTED = "contradictions_detected"
AWAITING_REPLY = "awaiting_reply"
PIPELINE_COMPLETE = "pipeline_complete"


# ---------------------------------------------------------------------------
# DAG-generation / job-terminal events (app/modules/execution_agent.py)
# ---------------------------------------------------------------------------
# ``blocked`` is yielded via ``_sse(status, ...)`` where the status string
# comes from a job-state variable rather than a literal — the inventory
# scanner won't see it as an emitter literal, so it lives here explicitly.

DAG_GENERATED = "dag_generated"
EXECUTION_FAILED = "execution_failed"
BLOCKED = "blocked"
# §17.855 (audit F6) — POST /jobs/{id}/advance (server-side auto-chain) frames.
# advance_phase {phase: research|planning} precedes each phase; advance_complete
# is the terminal frame when execute=false (execution replaces it otherwise).
ADVANCE_PHASE = "advance_phase"
ADVANCE_COMPLETE = "advance_complete"
# §17.624 — the hands-on assist gate parked the job as a plan (predominantly
# Shell/human DAG) instead of auto-executing it; a literal _sse("awaiting_assist"…).
AWAITING_ASSIST = "awaiting_assist"
# §17.777 — per-job token/cost budget cap reached; the executor hard-stops the
# job ('failed', error_summary 'cost_budget_exhausted') before the next node.
# Emitted as a literal _sse("budget_exhausted", …) in both the serial and
# parallel drivers.
BUDGET_EXHAUSTED = "budget_exhausted"


# ---------------------------------------------------------------------------
# Design-pipeline events (app/sim/design_pipeline.py)
# ---------------------------------------------------------------------------
# Per-stage start/done/error emitted by /design/{id}/advance?stage=…
# ``cancelled`` is a terminal event emitted by the §17.356 sticky-cancel
# guards when a /jobs/{id}/cancel landed mid-stage — a literal ``_sse(
# "cancelled", …)``, so the inventory scanner sees it and it must live here.

STAGE_START = "stage_start"
STAGE_DONE = "stage_done"
STAGE_ERROR = "stage_error"
CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Consumer-side synthesized events (pipelines/scaffold_router.py)
# ---------------------------------------------------------------------------
# ``stream_stalled`` is emitted by the OWUI consumer when N keepalive
# ticks pass without a real event arriving — it never originates on the
# orchestrator side. Tracked here so the inventory test (which scans the
# consumer) doesn't flag it as unregistered.

STREAM_STALLED = "stream_stalled"


# ---------------------------------------------------------------------------
# Progress / ETA events (§17.811)
# ---------------------------------------------------------------------------
# Uniform progress + ETA signal emitted by every long-running subsystem
# (execution_agent / research_agent / rag_pipeline / assist_agent /
# decomposition / design_pipeline). The data payload is a
# ``app.utils.progress.ProgressTracker.snapshot()`` dict:
# {phase, label, unit, completed, total, pct, elapsed_ms, eta_ms, eta_human,
#  rate_per_min, current_item, done_items, soft, summary}. Consumers that don't
# render it degrade gracefully — it never carries terminal state. Read-path /
# reconnect: the DAG path computes the snapshot ON DEMAND from ``dag_nodes``
# timestamps (no metadata write — the parallel frontier would race it, §17.811);
# the serial subsystems (research, decomposition) persist it to their state row.

PROGRESS = "progress"


# ---------------------------------------------------------------------------
# Generic / control events
# ---------------------------------------------------------------------------
# Used by every streaming endpoint regardless of source module.

DONE = "done"
ERROR = "error"
WARNING = "warning"
HEARTBEAT = "heartbeat"
QUEUED = "queued"


# ---------------------------------------------------------------------------
# Vocabulary snapshot — used by tests and the sync gate
# ---------------------------------------------------------------------------
# ``ALL_EVENT_NAMES`` is the canonical inventory. Any new event introduced
# on the orchestrator side MUST be added here in the same commit, or the
# inventory-scan test (test_sse_event_inventory.py) fails with the
# unmatched literal.

ALL_EVENT_NAMES = frozenset({
    # execution
    NODE_START, NODE_DONE, NODE_RETRY, NODE_FAILED, NODE_TOKEN,
    # assist
    ASSIST_HANDOFF_STARTED, ASSIST_HANDOFF_DONE, ASSIST_HANDOFF_NOOP,
    ASSIST_GUIDE_DELTA, ASSIST_GUIDE_DONE,
    # research
    RESEARCH_STARTED, RESEARCH_RESUMED, RESEARCH_COMPLETE,
    SEARCH_COMPLETE, RESEARCH_FETCH, EXTRACTION_COMPLETE, INGESTION_COMPLETE,
    DECOMPOSITION_COMPLETE, ITERATION_STARTED, ITERATION_COMPLETE,
    CONVERGENCE, GAP_ANALYSIS, CACHE_HIT_UPSTREAM,
    SOURCE_REF_RESOLVED, DISTILL_BYPASSED, CONTENT_TRUNCATED,
    EXTRACTOR_FALLBACK, QUALITY_GATE_FILTERED, CONTRADICTIONS_DETECTED,
    AWAITING_REPLY, PIPELINE_COMPLETE,
    # DAG / job-terminal
    DAG_GENERATED, EXECUTION_FAILED, BLOCKED, AWAITING_ASSIST, BUDGET_EXHAUSTED,
    # server-side auto-chain (§17.855)
    ADVANCE_PHASE, ADVANCE_COMPLETE,
    # design
    STAGE_START, STAGE_DONE, STAGE_ERROR, CANCELLED,
    # consumer-synthesized
    STREAM_STALLED,
    # progress / ETA (§17.811)
    PROGRESS,
    # generic
    DONE, ERROR, WARNING, HEARTBEAT, QUEUED,
})
