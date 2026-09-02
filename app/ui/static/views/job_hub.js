// §17.859 (audit G7) — the job hub: one job's whole life on one URL.
// Before this, a job's life spanned six peer views (approvals → plan → dag →
// theater → output → traces) with four copy-pasted job pickers and no home —
// the operator had to know the chain. #/job/:id is a persistent header strip
// (title · status · quick actions) over a tab row:
//   Overview · Plan · Run · Output · Traces · Costs
// Tab switches are plain hash navigations (#/job/:id/:tab) — the router
// re-renders the hub with the new tab active, so deep links, back/forward,
// and refresh all work with zero in-page tab state.
//
// The old per-view routes (#/theater/:id etc.) are gone (hard switch,
// operator decision) — every in-SPA link now points here.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, setCurrentJob } from "../util.js";
import { statusBadge, loading, errorPanel } from "../components.js";
import { flowGuide } from "./flow_guide.js";
import { briefPanel } from "./brief_panel.js";
import { renderTheater } from "./theater.js";
import { renderOutput } from "./output.js";
import { renderPlan } from "./plan.js";
import { renderJobTraces } from "./traces.js";
import { renderJobCosts } from "./costs.js";
import { renderApprovalDetail } from "./approvals.js";
import { renderChat } from "./assist.js";

const TABS = [
  ["overview", "Overview"],
  ["plan", "Plan"],
  ["run", "Run"],
  ["output", "Output"],
  ["traces", "Traces"],
  ["costs", "Costs"],
];

// Statuses where the job is driven through an assist session — the Run tab
// embeds the assist walkthrough instead of the autonomous theater. /assist/
// start is idempotent per job, so resolving the session this way is safe for
// these statuses ONLY (on an auto-mode job it would CONVERT it to assist).
const ASSIST_STATUSES = new Set([
  "assisted_executing", "assisted_running", "assisted_paused", "awaiting_assist",
]);

// The approval gate is a moment, not a place (operator decision): while the
// job sits at (or before) the gate, Overview IS the gate.
const GATE_STATUSES = new Set(["pending", "refining", "awaiting_confirmation"]);

export function jobHref(id, tab) {
  return tab && tab !== "overview" ? `#/job/${id}/${tab}` : `#/job/${id}`;
}

// ── Overview tab ─────────────────────────────────────────────────────
function renderOverview(container, jobId, job) {
  if (GATE_STATUSES.has(job.status)) {
    // Embed the approval gate detail (questions card, approve/reject chain).
    // It polls + navigates on approve by itself.
    return renderApprovalDetail(container, jobId);
  }
  const metaRow = (k, v) =>
    v == null || v === ""
      ? null
      : el("div", { class: "brief-field" },
          el("div", { class: "brief-key", text: k }),
          el("div", { class: "brief-val", text: String(v) }));
  mount(
    container,
    flowGuide(job, { here: `#/job/${jobId}` }),
    el(
      "div",
      { class: "card card-pad" },
      el("h3", { class: "brief-heading", text: "Job" }),
      el("div", { class: "brief-record" },
        metaRow("status", job.status),
        metaRow("domain", job.domain),
        metaRow("deliverable", job.deliverable_kind),
        metaRow("nodes", job.node_count),
        metaRow("created", timeAgo(job.created_at)),
        metaRow("updated", timeAgo(job.updated_at)),
        metaRow("completed", job.completed_at ? timeAgo(job.completed_at) : null)
      )
    ),
    // §17.843 receipt — the approval-gate answers as the server holds them.
    job.user_feedback
      ? el(
          "details",
          { class: "brief-details" },
          el("summary", {}, "✓ Approval-gate answers folded into research & plan"),
          el("pre", { class: "md-pre feedback-receipt", text: job.user_feedback })
        )
      : null,
    briefPanel(jobId)
  );
  return null;
}

// ── Run tab ──────────────────────────────────────────────────────────
function renderRun(container, jobId, job, ctx) {
  if (!ASSIST_STATUSES.has(job.status)) return renderTheater(container, jobId, ctx);
  // Assist-driven job: resolve the (idempotent, unique-per-job) session and
  // embed the walkthrough.
  let disposed = false;
  let childDispose = null;
  mount(container, loading("Opening assist session…"));
  (async () => {
    try {
      const s = await api.post("/assist/start", { job_id: jobId });
      if (disposed) return;
      const sid = s && (s.session_id || s.id);
      if (!sid) {
        mount(container, errorPanel({ message: "No assist session for this job." }));
        return;
      }
      childDispose = renderChat(container, String(sid));
    } catch (e) {
      if (!disposed) mount(container, errorPanel(e));
    }
  })();
  return () => {
    disposed = true;
    if (childDispose) childDispose();
  };
}

// ── Hub shell ────────────────────────────────────────────────────────
export default function jobHub(container, params) {
  const jobId = params && params.jobId;
  const tab = (params && params.tab) || "overview";
  let disposed = false;
  let childDispose = null;

  // §17.854 (audit G2) carried into the hub: the theater sets a guard message
  // while a run is streaming (leaving the surface disconnects the SSE stream,
  // which the server treats as cancel-by-design). Every hub-owned navigation
  // (tabs, back link) asks first.
  let navGuardMsg = null;
  const ctx = { setNavGuard: (msg) => (navGuardMsg = msg) };
  function guardClick(e) {
    if (navGuardMsg && !confirm(navGuardMsg)) e.preventDefault();
  }

  const titleEl = el("h1", { text: "Job" });
  const subEl = el("div", { class: "sub mono", text: shortId(jobId) });
  const pillSlot = el("span", {});
  const backLink = el("a", { class: "btn btn-sm btn-ghost", href: "#/jobs", text: "← Jobs" });
  backLink.addEventListener("click", guardClick);
  const compareLink = el("a", { class: "btn btn-sm btn-ghost", href: `#/compare/${jobId}`, text: "⚖ Compare" });
  compareLink.addEventListener("click", guardClick);

  const tabRow = el(
    "div",
    { class: "job-tabs", role: "tablist" },
    ...TABS.map(([key, label]) => {
      const a = el("a", {
        class: "job-tab" + (key === tab ? " active" : ""),
        href: jobHref(jobId, key),
        text: label,
        role: "tab",
        "aria-selected": key === tab ? "true" : "false",
      });
      a.addEventListener("click", guardClick);
      return a;
    })
  );

  const outlet = el("div", { class: "job-tab-outlet" }, loading("Loading…"));

  mount(
    container,
    el(
      "div",
      { class: "view-header job-hub-head" },
      el("div", { class: "row job-hub-title-row" },
        backLink,
        el("div", {}, titleEl, subEl)),
      el("div", { class: "header-actions" }, pillSlot, compareLink)
    ),
    tabRow,
    outlet
  );

  (async () => {
    let job = null;
    try {
      job = await api.get(`/jobs/${jobId}`);
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e));
      return;
    }
    if (disposed) return;
    titleEl.textContent = job.title || "(untitled)";
    mount(pillSlot, statusBadge(job.status));
    setCurrentJob(job); // §17.896 — pin it in the sidebar (⬡ DAG · ▶ Run · ▤ Output)

    switch (tab) {
      case "plan":
        childDispose = renderPlan(outlet, jobId);
        break;
      case "run":
        childDispose = renderRun(outlet, jobId, job, ctx);
        break;
      case "output":
        childDispose = renderOutput(outlet, jobId);
        break;
      case "traces":
        childDispose = renderJobTraces(outlet, jobId);
        break;
      case "costs":
        childDispose = renderJobCosts(outlet, jobId);
        break;
      default:
        childDispose = renderOverview(outlet, jobId, job);
    }
  })();

  return () => {
    disposed = true;
    if (childDispose) childDispose();
  };
}
