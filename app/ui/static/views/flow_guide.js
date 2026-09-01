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
    return { i: 1, hint: "The engine is refining your idea — the approval gate opens next.", action: { label: "Watch the approval gate", href: `#/job/${id}` } };
  if (st === "awaiting_confirmation")
    return { i: 1, hint: "Review the brief, answer what you can, then approve.", action: { label: "Open approval gate", href: `#/job/${id}` } };
  if (["researching", "planning"].includes(st))
    return { i: 2, hint: "Researching and drawing the plan — it lands in the Plan tab.", action: { label: "Open plan", href: `#/job/${id}/plan` } };
  if (st === "executing" && nodes > 0)
    return isAssist()
      ? {
          i: 2,
          hint: `Plan ready — ${nodes} step${nodes === 1 ? "" : "s"}. Start the walkthrough and I'll guide you through them one at a time, or review the plan first.`,
          action: { label: "✦ Start the walkthrough", href: `#/job/${id}/run`, start: true },
          secondary: { label: "Review & edit plan", href: `#/job/${id}/plan` },
        }
      : { i: 2, hint: "Plan ready, nothing run yet. Review or edit the steps, then execute — or drive it step-by-step with the assistant.", action: { label: "Review & edit plan", href: `#/job/${id}/plan` } };
  if (st === "running")
    return { i: 3, hint: "Execution in flight.", action: { label: "Watch the run", href: `#/job/${id}/run` } };
  if (["assisted_executing", "assisted_running"].includes(st))
    return { i: 3, hint: "Assist mode — you drive each step with the assistant guiding.", action: { label: "Open the walkthrough", href: `#/job/${id}/run` } };
  if (st === "awaiting_assist" || st === "assisted_paused")
    return { i: 3, hint: "Parked for assist mode — you drive each step with the assistant.", action: { label: "Open the walkthrough", href: `#/job/${id}/run` } };
  if (st === "completed")
    return { i: 4, hint: "Done — the compiled output is ready.", action: { label: "View output", href: `#/job/${id}/output` } };
  if (["failed", "blocked", "cancelled"].includes(st))
    return { i: 3, hint: `Job is ${st} — the Run tab has the recovery verbs (retry / resume).`, action: { label: "Open the run", href: `#/job/${id}/run` } };
  return { i: 0, hint: "", action: null };
}

/** Render the stepper. `activeHref` suppresses the action button when it
 * points at the surface it's mounted on (no "go where you already are"). */
export function flowGuide(job, { here = "" } = {}) {
  if (!job || !job.status) return null;
  const { i, hint, action, secondary } = flowState(job);
  // A `start` action is a verb — it stays visible even on the surface it
  // points at, because pressing it CHANGES the job rather than navigating.
  const showAction =
    action && (action.start || !(here && action.href.startsWith(here)));
  let actionEl = null;
  if (showAction && action.start) {
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
    el(
      "div",
      { class: "flow-steps" },
      ...STAGES.flatMap((label, idx) => [
        idx ? el("span", { class: "flow-sep", text: "→" }) : null,
        el("span", {
          class: "flow-step" + (idx < i ? " done" : idx === i ? " current" : ""),
          text: idx < i ? `✓ ${label}` : label,
        }),
      ])
    ),
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
