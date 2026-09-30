// §17.847 — the flow guide: where am I in the pipeline, what do I do next.
//
// Operator finding: "I had to follow the chain myself because I knew where to
// go — an average user won't." One compact stepper, mounted on every job
// surface, that shows the five stages with the current one lit and ONE
// primary next action derived from live job state.
import { el } from "./../util.js";
import { isAssist, startAssistFor } from "../exec_mode.js";
import * as api from "../api.js";
import { toast } from "../components.js";

const STAGES = ["Idea", "Approve", "Plan", "Run", "Output"];

/** Map a job to {stageIndex, hint, action:{label, href}|null}.
 *
 * §17.895 — an action may instead carry `start: true`, meaning "this is a
 * verb, not a destination": flowGuide renders it as a button that starts the
 * assist session. A plain href could only DROP the operator on the Run tab,
 * which for a job still at `executing` renders the autonomous theater — the
 * exact dead end that made the approve→assist progression feel missing. */
export function flowState(job) {
  const st = job.status;
  const nodes = job.node_count || 0;
  const id = job.id;
  // §17.859 — every action lands in the job hub (#/job/:id[/tab]).
  if (["pending", "refining"].includes(st))
    return { i: 1, hint: "The engine is refining your idea — it will have questions for you next.", action: { label: "Watch", href: `#/job/${id}` } };
  if (st === "awaiting_confirmation")
    return { i: 1, hint: "Review the brief, answer what you can, then approve.", action: { label: "Approve", href: `#/job/${id}` } };
  if (st === "planning" && nodes === 0 && stalledFor(job) > 2 * 60 * 1000)
    // §17.1036 — research finished, no plan, nothing moved for minutes: the
    // chain that should have planned is gone (a closed tab, a restart). The
    // server's resume loop will pick it up; this lets the operator do it now.
    return { i: 2, hint: "Research finished but the plan never started — resume planning and it lands on the Plan stage.", action: { label: "Resume planning", href: `#/job/${id}/plan`, resume: true } };
  if (["researching", "planning"].includes(st))
    return { i: 2, hint: "Researching and drawing the plan — it appears here when it is ready.", action: { label: "Open the plan", href: `#/job/${id}/plan` } };
  if (st === "executing" && nodes > 0)
    return isAssist()
      ? {
          i: 2,
          hint: `Plan ready — ${nodes} step${nodes === 1 ? "" : "s"}, nothing run yet. Start when you are ready, or review the steps first.`,
          action: { label: "✦ Start", href: `#/job/${id}/run`, start: true },
          secondary: { label: "Review the plan", href: `#/job/${id}/plan` },
        }
      : { i: 2, hint: "Plan ready, nothing run yet. Review the steps, then start.", action: { label: "Review the plan", href: `#/job/${id}/plan` } };
  if (st === "running")
    return { i: 3, hint: "The engine is working through the plan.", action: { label: "Watch the run", href: `#/job/${id}/run` } };
  if (["assisted_executing", "assisted_running"].includes(st))
    return { i: 3, hint: "You are walking through the plan, one step at a time.", action: { label: "Continue", href: `#/job/${id}/run` } };
  if (st === "awaiting_decision")
    return { i: 3, hint: "The run stopped to ask you a question — answer it and the engine carries on.", action: { label: "Decide", href: `#/job/${id}/run` } };
  if (st === "awaiting_assist" || st === "assisted_paused")
    return { i: 3, hint: "Parked for you — pick it up whenever you are ready.", action: { label: "Continue", href: `#/job/${id}/run` } };
  if (st === "completed")
    return { i: 4, hint: "Done — the output is ready.", action: { label: "Read the output", href: `#/job/${id}/output` } };
  if (["failed", "blocked", "cancelled"].includes(st))
    return { i: 3, hint: st === "cancelled" ? "Cancelled — the plan and its history are kept." : "The run stopped — the Run stage has the reason and the ways forward.", action: { label: st === "cancelled" ? "Open the run" : "See why", href: `#/job/${id}/run` } };
  return { i: 0, hint: "", action: null };
}

/** Render the stepper. `activeHref` suppresses the action button when it
 * points at the surface it's mounted on (no "go where you already are"). */
function stalledFor(job) {
  const ts = Date.parse(job.updated_at || job.created_at || "");
  return Number.isFinite(ts) ? Date.now() - ts : 0;
}

// §17.1214 — `steps:false` drops the five-stage strip and keeps the hint.
// Inside the job hub the strip is rendered ALREADY, at the top of the page, so
// the Run tab was showing the operator two of them — one saying Run is current
// while the badge beside it said "Waiting for your decision". The hint ("the run
// stopped to ask you a question") is the half that is not duplicated.
export function flowGuide(job, { here = "", steps = true } = {}) {
  if (!job || !job.status) return null;
  const { i, hint, action, secondary } = flowState(job);
  // A `start` action is a verb — it stays visible even on the surface it
  // points at, because pressing it CHANGES the job rather than navigating.
  const showAction =
    action && (action.start || action.resume || !(here && action.href.startsWith(here)));
  let actionEl = null;
  if (showAction && action.resume) {
    // §17.1036 — restart the server-owned chain (planning-only from here).
    const btn = el("button", { class: "btn btn-sm btn-primary", text: `${action.label} →` });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Resuming…";
      try {
        await api.post(`/jobs/${job.id}/approve`, {});
        toast("Planning resumed — the plan lands on the Plan stage.", "ok");
      } catch (e) {
        toast(`Could not resume: ${e.detail || e.message}`, "err");
        btn.disabled = false;
        btn.textContent = `${action.label} →`;
      }
    });
    actionEl = btn;
  } else if (showAction && action.start) {
    const btn = el("button", { class: "btn btn-sm btn-primary", text: `${action.label} →` });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Starting…";
      const ok = await startAssistFor(api, job.id, toast);
      if (!ok) {
        btn.disabled = false;
        btn.textContent = `${action.label} →`;
      }
    });
    actionEl = btn;
  } else if (showAction) {
    actionEl = el("a", { class: "btn btn-sm btn-primary", href: action.href, text: `${action.label} →` });
  }
  const showSecondary = secondary && !(here && secondary.href.startsWith(here));
  return el(
    "div",
    { class: "card flow-guide" },
    steps ? el(
      "div",
      { class: "flow-steps" },
      ...STAGES.flatMap((label, idx) => [
        idx ? el("span", { class: "flow-sep", text: "→" }) : null,
        el("span", {
          class: "flow-step" + (idx < i ? " done" : idx === i ? " current" : ""),
          text: idx < i ? `✓ ${label}` : label,
        }),
      ])
    ) : null,
    el(
      "div",
      { class: "row row-wrap flow-hint-row" },
      el("span", { class: "dim flow-hint", text: hint }),
      el("span", { class: "spacer" }),
      showSecondary ? el("a", { class: "btn btn-sm btn-ghost", href: secondary.href, text: secondary.label }) : null,
      actionEl
    )
  );
}
