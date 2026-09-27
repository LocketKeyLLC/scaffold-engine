// The operator vocabulary — ONE place that turns the engine's enums into plain
// English. Before this, `assisted_running`, `plan_only`, `awaiting_assist`,
// `eng_design` and node keys like `ADD65` were printed raw on every surface
// (the status pill, the job rows, the output header). A status pill keeps its
// `st-<status>` class for colour; only the words change.
//
// Pure: no DOM, no api — tests/ui/vocab.test.mjs pins every job status.

// Job statuses (app/modules/job_state.py JOB_STATUSES) → what the operator reads.
export const STATUS_LABEL = {
  pending: "Queued",
  refining: "Refining your idea",
  awaiting_confirmation: "Waiting for your approval",
  researching: "Researching",
  planning: "Drawing the plan",
  executing: "Plan ready — not started",
  running: "Running",
  aggregating: "Combining results",
  awaiting_assist: "Ready to walk through",
  awaiting_decision: "Waiting for your decision",
  assisted_executing: "In progress",
  assisted_running: "In progress",
  assisted_paused: "Paused",
  blocked: "Blocked — needs you",
  failed: "Failed",
  cancelled: "Cancelled",
  completed: "Done",
  // node / step / session vocabularies that share the pill
  done: "Done",
  skipped: "Skipped",
  active: "Active",
  paused: "Paused",
  abandoned: "Abandoned",
  awaiting_reply: "Waiting for your reply",
  error: "Error",
  committed: "Done",
  presented: "Current step",
  awaiting_input: "Needs your answer",
  handed_off: "Engine did it",
};

export function statusLabel(status) {
  const s = String(status || "").trim();
  if (!s) return "Unknown";
  return STATUS_LABEL[s] || s.replace(/_/g, " ");
}

export const DELIVERABLE_LABEL = {
  plan_only: "Plan (not executed)",
  code: "Code",
  document: "Document",
  runbook: "Runbook",
  report: "Report",
  mixed: "Mixed deliverable",
};

export function deliverableLabel(kind) {
  const k = String(kind || "").trim();
  if (!k) return "";
  return DELIVERABLE_LABEL[k] || k.replace(/_/g, " ");
}

// The three Home buckets. "Needs you" is what an operator opens the app for;
// "in progress" is the engine working; "done" is history.
export const NEEDS_YOU = new Set(["awaiting_confirmation", "awaiting_assist", "awaiting_decision", "assisted_paused", "blocked", "failed"]);
export const IN_PROGRESS = new Set(["pending", "refining", "researching", "planning", "executing", "running", "assisted_executing", "assisted_running", "aggregating"]);
export const DONE = new Set(["completed", "cancelled"]);

export function bucketOf(status) {
  if (NEEDS_YOU.has(status)) return "needs_you";
  if (IN_PROGRESS.has(status)) return "in_progress";
  if (DONE.has(status)) return "done";
  return "in_progress";
}

// The ONE thing to do next for a job, from its status. Every status maps to
// exactly one verb and one destination; `primary` is false when the verb is a
// look ("Watch") rather than an act ("Approve", "Continue").
// §17.859 — every destination is a job-page stage (#/job/:id[/stage]).
export function nextStep(job, { progress } = {}) {
  const id = job.id;
  const st = job.status;
  const nodes = job.node_count || 0;
  const pos = progress && progress.total ? ` — step ${Math.min(progress.total, (progress.completed || 0) + 1)} of ${progress.total}` : "";
  switch (st) {
    case "awaiting_confirmation":
      return { label: "Approve", href: `#/job/${id}`, primary: true, hint: "The brief is ready and the engine has questions for you." };
    case "pending":
    case "refining":
      return { label: "Watch", href: `#/job/${id}`, primary: false, hint: "Refining your idea into a brief — usually 1–9 min. Safe to leave." };
    case "researching":
      return { label: "Watch", href: `#/job/${id}/plan`, primary: false, hint: "Researching before drawing the plan." };
    case "planning":
      return { label: "Watch", href: `#/job/${id}/plan`, primary: false, hint: "Drawing the plan from the brief and the research." };
    case "executing":
      return nodes > 0
        ? { label: "Start", href: `#/job/${id}/plan`, primary: true, hint: `${nodes} step${nodes === 1 ? "" : "s"} planned, nothing run yet.` }
        : { label: "Open", href: `#/job/${id}`, primary: false, hint: "" };
    case "running":
      return { label: "Watch the run", href: `#/job/${id}/run`, primary: false, hint: "The engine is working through the plan." };
    case "aggregating":
      return { label: "Watch", href: `#/job/${id}`, primary: false, hint: "Combining the component results." };
    case "awaiting_assist":
      return { label: "Start walkthrough", href: `#/job/${id}/run`, primary: true, hint: "The plan is parked for you to walk through step by step." };
    case "awaiting_decision":
      return { label: "Decide", href: `#/job/${id}/run`, primary: true, hint: "The run stopped to ask you a question — answer it and it continues." };
    case "assisted_executing":
    case "assisted_running":
      return { label: "Continue", href: `#/job/${id}/run`, primary: true, hint: `Walking through the plan${pos}.` };
    case "assisted_paused":
      return { label: "Resume", href: `#/job/${id}/run`, primary: true, hint: `Paused${pos}. Pick up where you left off.` };
    case "blocked":
      return { label: "Fix", href: `#/job/${id}/run`, primary: true, hint: "A step is blocked — the run page has the reason and the ways forward." };
    case "failed":
      return { label: "See why", href: `#/job/${id}/run`, primary: true, hint: "The run stopped. The run page has the reason and a retry." };
    case "completed":
      return { label: "Read output", href: `#/job/${id}/output`, primary: false, hint: "Finished — the deliverable is compiled." };
    case "cancelled":
      return { label: "Open", href: `#/job/${id}`, primary: false, hint: "Cancelled. Its brief and history are kept." };
    default:
      return { label: "Open", href: `#/job/${id}`, primary: false, hint: "" };
  }
}
