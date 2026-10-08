// The job page — one job's whole life on one URL, and the STAGE STRIP is the
// navigation.
//
//   Idea → Approve → Plan → Run → Output
//
// #/job/:id opens on the job's CURRENT stage (awaiting approval → the gate;
// planning → the plan; walking through → the run; done → the output). Each
// stage is a link; finished stages read ✓; stages the job has not reached are
// not yet clickable. Brief, traces, costs and compare live behind ⋯ (Details),
// shown once, instead of on two tabs at the same time.
//
// UX overhaul (2026-09-27). Before this the page was a six-tab hub whose
// Overview was a metadata table with the raw status, whose Plan tab buried the
// DAG under a toolbar, a stage strip, a two-paragraph explainer and the brief,
// and whose Overview and Plan both embedded the whole brief editor. §17.859
// made one URL per job; this makes the page read like the flow.
import * as api from "../api.js";
import { jobStore } from "../store.js";
import { el, mount, shortId, timeAgo, mdToHtml, setCurrentJob, copy } from "../util.js";
import { statusBadge, loading, errorPanel, toast } from "../components.js";
import { flowState } from "./flow_guide.js";
import { briefPanel } from "./brief_panel.js";
import { renderTheater } from "./theater.js";
import { renderOutput } from "./output.js";
import { renderPlan } from "./plan.js";
import { renderJobTraces } from "./traces.js";
import { renderJobCosts } from "./costs.js";
import { renderApprovalDetail, progressLine } from "./approvals.js";
import * as router from "../router.js";
import { renderChat } from "./assist.js";
import { isAssist, startAssistFor } from "../exec_mode.js";
import { deliverableLabel } from "../vocab.js";

export const STAGES = [
  ["idea", "Idea"],
  ["approve", "Approve"],
  ["plan", "Plan"],
  ["run", "Run"],
  ["output", "Output"],
];
const STAGE_KEYS = STAGES.map(([k]) => k);

// §17.1011 — synonyms an operator (or an old link) may use for a real pane.
// Every value MUST be a key in KNOWN_TABS; `resolveTab` is the single resolver.
// `auto` = "the stage the job is at" (the old Overview address lands there).
export const TAB_ALIASES = {
  assist: "run", walkthrough: "run", theater: "run", exec: "run",
  execute: "run", results: "output", dag: "plan", nodes: "plan",
  overview: "auto", gate: "approve", approval: "approve", brief: "details",
};

export function resolveTab(raw) {
  const t = raw || "auto";
  return TAB_ALIASES[t] || t;
}

// §17.1114 (ledger U-1) — the panes that exist. An unknown one still renders
// the job's current stage (nothing else is sensible) but SAYS so.
export const KNOWN_TABS = ["auto", ...STAGE_KEYS, "details", "traces", "costs", "follow", "full"];

export function unknownTabNotice(raw) {
  if (!raw) return "";
  const t = resolveTab(raw);
  return KNOWN_TABS.includes(t) ? "" : `No stage named “${raw}” on this job — showing where it is.`;
}

// Statuses where the job is driven through an assist session — the Run stage
// embeds the assist walkthrough instead of the autonomous theater. /assist/
// start is idempotent per job, so resolving the session this way is safe for
// these statuses ONLY (on an auto-mode job it would CONVERT it to assist).
const ASSIST_STATUSES = new Set([
  "assisted_executing", "assisted_running", "assisted_paused", "awaiting_assist",
]);

// §17.1433 — a job the engine stopped on (blocked, failed, or waiting on a decision) must still let
// the operator be WALKED THROUGH its remaining steps. Live, 2026-10-08: the router walkthrough (ADD4)
// was the first open step of a `blocked` job, and the page offered only "Fix" -> the autonomous run
// view; "Walk me through it" existed only on the Plan stage before a job's first run. No path at all.
const WALKTHROUGH_OFFERED = new Set(["blocked", "failed", "awaiting_decision"]);
export function walkthroughOffered(status) {
  return WALKTHROUGH_OFFERED.has(String(status || ""));
}

// The approval gate is a moment, not a place (operator decision): while the
// job sits at (or before) the gate, Idea and Approve are the same page.
export const GATE_STATUSES = new Set(["pending", "refining", "awaiting_confirmation"]);

/** Pure: the stage a job is AT — where #/job/:id opens. */
export function stageFor(job) {
  const st = job && job.status;
  const nodes = (job && job.node_count) || 0;
  if (GATE_STATUSES.has(st)) return "approve";
  if (st === "completed") return "output";
  if (["researching", "planning", "executing"].includes(st)) return "plan";
  if (st === "cancelled") return nodes > 0 ? "plan" : "idea";
  return "run";   // running, assisted_*, awaiting_assist, blocked, failed, aggregating
}

/** Pure: which stages are open to click. Reached stages always; ahead only
 *  when there is something there (a plan with nodes, a compiled output). */
export function stageAccess(job) {
  const cur = STAGE_KEYS.indexOf(stageFor(job));
  const nodes = (job && job.node_count) || 0;
  return Object.fromEntries(STAGE_KEYS.map((k, i) => {
    if (i <= cur) return [k, true];
    if (k === "plan") return [k, nodes > 0];
    if (k === "run") return [k, nodes > 0 && !GATE_STATUSES.has(job.status)];
    if (k === "output") return [k, !!(job && job.has_compiled_output)];
    return [k, false];
  }));
}

export function jobHref(id, tab) {
  return tab && tab !== "auto" ? `#/job/${id}/${tab}` : `#/job/${id}`;
}

// ── Idea (a job past the gate) ────────────────────────────────────────
function renderIdea(container, jobId, job) {
  const brief = job.refined_brief || {};
  mount(
    container,
    el("div", { class: "card card-pad brief-block" },
      el("h3", { class: "brief-heading", text: "What you asked for" }),
      job.input_text ? el("div", { class: "md brief-prose", html: mdToHtml(job.input_text) }) : el("p", { class: "dim", text: "No original request recorded." })),
    brief.description
      ? el("div", { class: "card card-pad brief-block" },
          el("h3", { class: "brief-heading", text: "What the engine understood" }),
          el("div", { class: "md brief-prose", html: mdToHtml(brief.description) }))
      : null,
    // §17.843 receipt — the approval-gate answers as the server holds them.
    job.user_feedback
      ? el("details", { class: "brief-details" },
          el("summary", {}, "✓ Your answers from the approval gate"),
          el("pre", { class: "md-pre feedback-receipt", text: job.user_feedback }))
      : null,
    el("p", { class: "dim", text: "Change the brief the engine plans and guides from under ⋯ › Details." })
  );
  return null;
}

// ── Details (⋯): the living brief + the job's facts ──────────────────
function renderDetails(container, jobId, job) {
  const fact = (k, v) => v == null || v === "" ? null
    : el("div", { class: "brief-field" }, el("div", { class: "brief-key", text: k }), el("div", { class: "brief-val", text: String(v) }));
  mount(
    container,
    briefPanel(jobId),
    el("div", { class: "card card-pad" },
      el("h3", { class: "brief-heading", text: "This job" }),
      el("div", { class: "brief-record" },
        fact("deliverable", deliverableLabel(job.deliverable_kind)),
        fact("domain", job.domain),
        fact("steps", job.node_count),
        // §17.1008 — the decomposition breadcrumb.
        job.component_index != null ? fact("part of", `component ${job.component_index}`) : null,
        fact("created", timeAgo(job.created_at)),
        fact("updated", timeAgo(job.updated_at)),
        fact("finished", job.completed_at ? timeAgo(job.completed_at) : null),
        fact("job id", job.id)),
      el("div", { class: "row row-wrap drawer-actions" },
        el("button", { class: "btn btn-sm btn-ghost", text: "Copy job id", onClick: async () => toast((await copy(job.id)) ? "Job id copied." : "Copy failed.", "") }),
        el("a", { class: "btn btn-sm btn-ghost", href: `#/compare/${jobId}`, text: "Compare with another job" }))),
    // §17.1008 — a link back to the umbrella, not just a note that one exists.
    job.parent_job_id
      ? el("div", { class: "card card-pad umbrella-link" },
          el("span", { text: "This job is one component of a larger build. " }),
          el("a", { href: `#/job/${job.parent_job_id}`, text: "Open the umbrella job →" }))
      : null,
    job.user_feedback
      ? el("details", { class: "brief-details" },
          el("summary", {}, "✓ Approval-gate answers (folded into research & plan)"),
          el("pre", { class: "md-pre feedback-receipt", text: job.user_feedback }))
      : null
  );
  return null;
}

// ── Plan, while it is still being drawn ──────────────────────────────
// Between approval and the plan there are minutes of research + planning.
// Leaving the gate (or reloading) used to land on an EMPTY canvas that never
// updated. This pane shows the chain's own progress line, polls the job, and
// re-opens the page on the Plan stage the moment the status moves on.
export const DRAWING_STATUSES = new Set(["researching", "planning"]);
export function planIsDrawing(job) {
  return !!job && DRAWING_STATUSES.has(job.status) && !(job.node_count > 0);
}
function renderDrawing(container, jobId, job) {
  let disposed = false;
  const line = el("span", { class: "progress-msg", text: job.status === "researching" ? "Researching before drawing the plan…" : "Drawing the plan…" });
  const startedAt = Date.parse(job.updated_at || job.created_at || "");
  const meta = el("div", { class: "wait-meta" });
  function paintMeta() {
    const mins = startedAt ? Math.max(0, Math.floor((Date.now() - startedAt) / 60000)) : null;
    mount(meta,
      mins != null ? el("span", { class: "wait-elapsed mono", text: `${mins}m elapsed` }) : null,
      el("span", { class: "faint", text: `${mins != null ? "· " : ""}usually a few minutes · this page updates itself` }));
  }
  paintMeta();
  mount(container,
    el("div", { class: "card card-pad drawing-card" },
      el("div", { class: "approval-progress" }, el("span", { class: "spin" }), line),
      meta,
      el("p", { class: "dim wait-leave" }, "Safe to leave — the job keeps going, and Home will say when the plan is ready.")));
  async function tick() {
    if (disposed || document.hidden) return;
    try {
      const [st, fresh] = await Promise.all([
        api.get(`/jobs/${jobId}/approve`).catch(() => null),
        jobStore.get(jobId, { fresh: true }),
      ]);
      if (disposed) return;
      if (st) { const t = progressLine(st); if (t) line.textContent = t; }
      paintMeta();
      if (!planIsDrawing(fresh)) {
        window.dispatchEvent(new CustomEvent("scaffold:job-status", { detail: { jobId, status: fresh.status } }));
        router.redispatch();   // the page re-opens on the stage the job is at now
      }
    } catch (e) {
      line.textContent = `Could not reach the engine (${e.detail || e.message}) — retrying…`;
    }
  }
  const timer = setInterval(tick, 4000);   // registered in tests/test_spa_poll_modal_wiring.py
  return () => { disposed = true; clearInterval(timer); };
}

// ── Run ──────────────────────────────────────────────────────────────
function renderRun(container, jobId, job, ctx, opts = {}) {
  if (!ASSIST_STATUSES.has(job.status)) {
    if (!walkthroughOffered(job.status)) return renderTheater(container, jobId, ctx);
    // §17.1433 — the run view, with the walkthrough one click away above it
    const host = el("div");
    const btn = el("button", { class: "btn btn-sm btn-primary", text: "✦ Walk me through it",
      title: "You do each remaining step yourself; the engine guides you screen by screen and checks the result." });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const was = btn.textContent;
      btn.textContent = "Starting…";
      const ok = await startAssistFor(api, jobId, toast);
      if (!ok) { btn.disabled = false; btn.textContent = was; }
    });
    mount(container, el("div", {},
      el("div", { class: "row row-wrap stage-hint-row" },
        el("span", { class: "dim stage-hint", text: "Some remaining steps are yours to do (a router app, a phone, a screen the engine cannot reach). The engine can walk you through them." }),
        el("span", { class: "spacer" }), btn),
      host));
    return renderTheater(host, jobId, ctx);
  }
  // §17.1209 — RESOLVE the session, do not start one. This was
  // `POST /assist/start` on the comment that it was "idempotent,
  // unique-per-job". It is idempotent about creating a session and about
  // nothing else: the resume path runs §17.1052's stranded-session repair,
  // which finalizes a session whose steps are all terminal — and §17.1208
  // showed what that did to the job behind it. Opening this page took the
  // operator's job from `blocked` to `completed` over 21 pending and 2 failed
  // nodes. Looking at a job is a read; only a click starts anything.
  let disposed = false;
  let childDispose = null;
  mount(container, loading("Opening the walkthrough…"));
  (async () => {
    try {
      const s = await api.get(`/assist/for-job/${encodeURIComponent(jobId)}`);
      if (disposed) return;
      const sid = s && (s.session_id || s.id);
      if (!sid) {
        mount(container, errorPanel({ message: "No assist session for this job." }));
        return;
      }
      childDispose = renderChat(container, String(sid), { embedded: true, follow: !!opts.follow });  // §17.1055 · §17.1160
    } catch (e) {
      if (!disposed) mount(container, errorPanel(e));
    }
  })();
  return () => {
    disposed = true;
    if (childDispose) childDispose();
  };
}

// ── The stage strip ──────────────────────────────────────────────────
function stageStrip(job, jobId, active) {
  const cur = STAGE_KEYS.indexOf(stageFor(job));
  const access = stageAccess(job);
  const strip = el("div", { class: "stage-strip", role: "tablist", "aria-label": "Stages", onKeydown: (e) => {   // §17.1118 — arrow keys move between stages
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const tabs = Array.from(e.currentTarget.querySelectorAll('[role="tab"]:not([aria-disabled="true"])'));
    const i = tabs.indexOf(document.activeElement);
    if (i === -1) return;
    e.preventDefault();
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    next.focus(); next.click();
  } });
  STAGES.forEach(([key, label], i) => {
    const state = i < cur ? "done" : i === cur ? "current" : "ahead";
    const open = access[key];
    const cls = `stage stage-${state}` + (key === active ? " active" : "") + (open ? "" : " locked");
    const node = open
      ? el("a", { class: cls, href: jobHref(jobId, key), role: "tab", "aria-selected": key === active ? "true" : "false" })
      : el("span", { class: cls, role: "tab", "aria-disabled": "true", "aria-selected": "false", title: "Not there yet" });
    node.append(el("span", { class: "stage-mark", text: state === "done" ? "✓" : state === "current" ? "●" : "○" }), el("span", { class: "stage-label", text: label }));
    if (i) strip.append(el("span", { class: "stage-sep", "aria-hidden": "true" }));
    strip.append(node);
  });
  return strip;
}

// The one line under the strip: where the job is and the one thing to do —
// from flow_guide's state (the hints and verbs the operator already knows),
// suppressed when it points at the pane that is open.
function stageHint(job, jobId, active) {
  const { hint, action, secondary } = flowState(job);
  // an action whose destination is the open stage is noise ("Approve →" on the gate)
  const stageOfHref = (href) => {
    const m = /#\/job\/[^/]+\/?([^/?#]*)/.exec(href || "");
    const t = resolveTab(m ? m[1] : "");
    return t === "auto" ? stageFor(job) : t;
  };
  const pointsHere = (a) => a && a.href && stageOfHref(a.href) === active;
  let actionEl = null;
  if (action && (action.start || action.resume || !pointsHere(action))) {
    if (action.resume) {
      const btn = el("button", { class: "btn btn-sm btn-primary", text: `${action.label} →` });
      btn.addEventListener("click", async () => {
        btn.disabled = true; btn.textContent = "Resuming…";
        try { await api.post(`/jobs/${job.id}/approve`, {}); toast("Planning resumed — the plan lands on the Plan stage.", "ok"); }
        catch (e) { toast(`Could not resume: ${e.detail || e.message}`, "err"); btn.disabled = false; btn.textContent = `${action.label} →`; }
      });
      actionEl = btn;
    } else if (action.start) {
      actionEl = startControl(job.id);
    } else {
      actionEl = el("a", { class: "btn btn-sm btn-primary", href: action.href, text: `${action.label} →` });
    }
  }
  // the Plan stage's toolbar owns the Start control — never two Start buttons
  if (active === "plan" && action && action.start) actionEl = null;
  const secEl = secondary && !pointsHere(secondary) ? el("a", { class: "btn btn-sm btn-ghost", href: secondary.href, text: secondary.label }) : null;
  if (!hint && !actionEl && !secEl) return null;
  return el("div", { class: "row row-wrap stage-hint-row" },
    el("span", { class: "dim stage-hint", text: hint }), el("span", { class: "spacer" }), secEl, actionEl);
}

// §17.853 — the Auto/Assist decision, made where it is taken: the start
// control is a split button — the primary verb reads the mode, the small
// chevron swaps it. Exported for the Plan stage's toolbar.
export function startControl(jobId, { size = "btn-sm" } = {}) {
  const { setExecMode } = execModeApi;
  const label = () => (isAssist() ? "✦ Walk me through it" : "▶ Let the engine run it");
  const main = el("button", { class: `btn ${size} btn-primary start-main`, text: label() });
  main.addEventListener("click", async () => {
    main.disabled = true;
    const was = main.textContent;
    main.textContent = "Starting…";
    if (isAssist()) {
      const ok = await startAssistFor(api, jobId, toast);
      if (!ok) { main.disabled = false; main.textContent = was; }
    } else {
      sessionStorage.setItem("scaffold_autorun", jobId);   // §17.818 — the theater picks it up
      location.hash = `#/job/${jobId}/run`;
    }
  });
  const menu = el("details", { class: "start-menu" },
    el("summary", { class: `btn ${size} btn-primary start-caret`, "aria-label": "Choose how to run this plan", title: "Choose how to run this plan", text: "▾" }),
    el("div", { class: "start-menu-body" },
      el("button", { class: "btn btn-sm btn-ghost", text: "✦ Walk me through it", title: "You run each step on your machines; the engine guides, verifies and adapts. It never touches your hardware.",
        onClick: () => { setExecMode("assist"); main.textContent = label(); menu.open = false; } }),
      el("button", { class: "btn btn-sm btn-ghost", text: "▶ Let the engine run it", title: "The engine works every step itself and produces runbooks, configs and code. It connects to a machine only if you opened the write channel, and then it pauses at each step that would change one and asks you to approve the commands first.",
        onClick: () => { setExecMode("auto"); main.textContent = label(); menu.open = false; } })));
  return el("div", { class: "row start-control" }, main, menu);
}
import * as execModeApi from "../exec_mode.js";

// ── The page ─────────────────────────────────────────────────────────
export default function jobHub(container, params) {
  const jobId = params && params.jobId;
  const requested = resolveTab(params && params.tab);
  let disposed = false;
  let childDispose = null;
  let statusListener = null;

  // §17.1007 — the run is a detached background task (run_broker); navigating
  // away costs nothing, so no nav guard. `setNavGuard` stays on ctx as a no-op:
  // renderTheater is also mounted from outside the hub.
  const ctx = { setNavGuard: () => {} };

  const titleEl = el("h1", { class: "job-title", text: "Job" });
  const pillSlot = el("span", {});
  const backLink = el("a", { class: "btn btn-sm btn-ghost", href: "#/", text: "← Home", "aria-label": "Back to Home" });
  const moreMenu = el("details", { class: "job-more" },
    el("summary", { class: "btn btn-sm btn-ghost", text: "⋯", "aria-label": "More: details, traces, costs", title: "Details, traces, costs, classic view" }),
    el("div", { class: "job-more-body" },
      el("a", { class: "btn btn-sm btn-ghost", href: jobHref(jobId, "details"), text: "Details & brief" }),
      el("a", { class: "btn btn-sm btn-ghost", href: jobHref(jobId, "traces"), text: "Model traces" }),
      el("a", { class: "btn btn-sm btn-ghost", href: jobHref(jobId, "costs"), text: "Costs" }),
      el("a", { class: "btn btn-sm btn-ghost", href: `#/compare/${jobId}`, text: "Compare" }),
      el("a", { class: "btn btn-sm btn-ghost", href: jobHref(jobId, "full"), text: "Classic walkthrough view" })));
  const onDocClick = (e) => { if (moreMenu.open && !moreMenu.contains(e.target)) moreMenu.open = false; };
  document.addEventListener("click", onDocClick);

  const stripSlot = el("div", { class: "stage-slot" });
  const outlet = el("div", { class: "job-tab-outlet" }, loading("Loading…"));
  const tabNoticeText = unknownTabNotice(params && params.tab);
  const tabNotice = tabNoticeText ? el("div", { class: "card warn-inline", text: "⚠ " + tabNoticeText }) : null;

  mount(
    container,
    el("div", { class: "view-header job-hub-head" },
      el("div", { class: "row job-hub-title-row" }, backLink, titleEl),
      el("div", { class: "header-actions" }, pillSlot, moreMenu)),
    stripSlot,
    tabNotice,
    outlet
  );

  (async () => {
    let job = null;
    try {
      job = await jobStore.get(jobId);
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e));
      return;
    }
    if (disposed) return;
    titleEl.textContent = job.title || "(untitled)";
    mount(pillSlot, statusBadge(job.status));
    const tab = requested === "auto" || !KNOWN_TABS.includes(requested) ? stageFor(job) : requested;
    // an "approve" link on a job past the gate reads its idea; "idea" at the gate IS the gate
    const pane = GATE_STATUSES.has(job.status) && (tab === "idea" || tab === "approve") ? "approve"
      : tab === "approve" && !GATE_STATUSES.has(job.status) ? "idea" : tab;
    const isFollow = pane === "run" || pane === "follow";   // §17.1161 — Run is the Follow layout
    container.classList.toggle("follow-mode", isFollow);
    const stripFor = STAGE_KEYS.includes(pane) ? pane : "auto";
    mount(stripSlot, stageStrip(job, jobId, stripFor), isFollow ? null : stageHint(job, jobId, stripFor));

    // §17.1052 — the Run stage finishes the job in place (last step ✓, or a
    // stranded session finalized on reopen); the pill was rendered once from
    // the job row and read "in progress" next to a "completed" hero.
    const onStatus = (ev) => {
      if (disposed || !ev.detail || ev.detail.jobId !== jobId) return;
      job.status = ev.detail.status;
      mount(pillSlot, statusBadge(job.status));
      mount(stripSlot, stageStrip(job, jobId, stripFor), isFollow ? null : stageHint(job, jobId, stripFor));
    };
    window.addEventListener("scaffold:job-status", onStatus);
    statusListener = onStatus;
    setCurrentJob(job); // §17.896 — the rail's "Current" link

    switch (pane) {
      case "approve":
        childDispose = renderApprovalDetail(outlet, jobId);
        break;
      case "idea":
        childDispose = renderIdea(outlet, jobId, job);
        break;
      case "plan":
        childDispose = planIsDrawing(job) ? renderDrawing(outlet, jobId, job) : renderPlan(outlet, jobId, { job });
        break;
      case "run":      // §17.1161 — the Follow layout IS the walkthrough
      case "follow":   // §17.1160 — kept as an alias so links keep working
        childDispose = renderRun(outlet, jobId, job, ctx, { follow: true });
        break;
      case "full":     // §17.1161 — the classic Run layout (step card, whole-session scroll, all verbs)
        childDispose = renderRun(outlet, jobId, job, ctx);
        break;
      case "output":
        childDispose = renderOutput(outlet, jobId);
        break;
      case "details":
        childDispose = renderDetails(outlet, jobId, job);
        break;
      case "traces":
        childDispose = renderJobTraces(outlet, jobId);
        break;
      case "costs":
        childDispose = renderJobCosts(outlet, jobId);
        break;
      default:
        childDispose = renderIdea(outlet, jobId, job);
    }
  })();

  return () => {
    disposed = true;
    container.classList.remove("follow-mode");
    document.removeEventListener("click", onDocClick);
    if (statusListener) window.removeEventListener("scaffold:job-status", statusListener);
    if (childDispose) childDispose();
  };
}
