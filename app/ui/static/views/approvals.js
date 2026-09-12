// Approval gate. Lists jobs halted at `awaiting_confirmation` and renders the
// Phase-1 refined brief + feasibility for review. Approve runs the two-stage
// chain (POST /ideate/confirm → POST /dag, progress polled) then lands in the
// plan editor with nothing executed; Reject cancels the job (non-destructive).
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount, shortId, timeAgo, mdToHtml } from "../util.js";
import { statusBadge, loading, errorPanel, toast, emptyState } from "../components.js";
import { flowGuide } from "./flow_guide.js";
import { isAssist, startAssistFor, onExecModeChange } from "../exec_mode.js";

// Phase 1 in flight — feasibility not ready for approval yet (e.g. a job just
// submitted from the composer). The detail view waits + polls through these
// rather than dead-ending.
const PRE_APPROVAL = new Set(["pending", "refining"]);

// ── Render an arbitrary refined_brief / feasibility record safely ────────
function renderValue(v) {
  if (v == null || v === "") return el("span", { class: "dim", text: "—" });
  if (Array.isArray(v)) {
    if (!v.length) return el("span", { class: "dim", text: "—" });
    return el(
      "ul",
      { class: "brief-list" },
      ...v.map((item) =>
        el("li", {}, typeof item === "object" ? renderValue(item) : el("span", { text: String(item) }))
      )
    );
  }
  if (typeof v === "object") {
    // nested object → pretty JSON, textContent (never html) so it's injection-safe
    return el("pre", { class: "md-pre json-pre", text: JSON.stringify(v, null, 2) });
  }
  const s = String(v);
  // long prose → markdown; short scalars → plain text
  return s.length > 80 || /\n/.test(s)
    ? el("div", { class: "md brief-prose", html: mdToHtml(s) })
    : el("span", { text: s });
}

function renderRecord(obj, omit = new Set()) {
  const entries = Object.entries(obj || {}).filter(([k]) => !omit.has(k));
  if (!entries.length) return null;
  return el(
    "div",
    { class: "brief-record" },
    ...entries.map(([k, v]) =>
      el(
        "div",
        { class: "brief-field" },
        el("div", { class: "brief-key", text: k.replace(/_/g, " ") }),
        el("div", { class: "brief-val" }, renderValue(v))
      )
    )
  );
}

// Collapsed-by-default section with a count in the summary line. Native
// <details> — no JS state, works under the strict CSP.
function collapsible(title, count, node) {
  if (!node) return null;
  return el(
    "details",
    { class: "brief-details" },
    el("summary", {}, `${title}${count != null ? ` (${count})` : ""}`),
    node
  );
}

function listOrNull(arr) {
  return Array.isArray(arr) && arr.length ? renderValue(arr) : null;
}

// The operator-facing decision layer: everything the engine wants ANSWERED,
// pulled out of the two records it hides in (brief.ambiguities +
// feasibility.clarifications_needed) and rendered as first-class questions
// with an answer box. Answers travel as /ideate/confirm feedback and are
// folded into the research + plan (§17.820 whitespace→None server-side).
// The two source fields (brief.ambiguities, feasibility.clarifications_needed)
// routinely restate the same question in different words — exact-match dedupe
// isn't enough. Token-overlap Jaccard: ≥0.5 shared distinctive tokens → same
// question; keep the longer (usually more specific) phrasing.
function dedupeQuestions(qs) {
  const toks = (s) => new Set(s.toLowerCase().split(/[^a-z0-9]+/).filter((t) => t.length > 3));
  const kept = [];
  for (const q of qs) {
    const qt = toks(q);
    const dup = kept.findIndex((k) => {
      const kt = toks(k);
      const inter = [...qt].filter((t) => kt.has(t)).length;
      return inter / Math.max(1, Math.min(qt.size, kt.size)) >= 0.5;
    });
    if (dup === -1) kept.push(q);
    else if (q.length > kept[dup].length) kept[dup] = q;
  }
  return kept;
}

// §17.1007 — confidence, bound to a consequence.
//
// A bare "78%" invites over-reliance from confident operators and dismissal
// from sceptical ones, because nothing on screen says what 78 licenses that 45
// would not. Three bands, each naming the action it recommends. The thresholds
// are deliberately coarse: the underlying number is a model's self-report, and
// implying more resolution than that would be its own kind of lie.
const CONF_BANDS = [
  { min: 0.75, key: "high", label: "high confidence" },
  { min: 0.45, key: "med", label: "moderate confidence" },
  { min: 0, key: "low", label: "low confidence" },
];

function confidenceBand(c) {
  const v = typeof c === "number" ? c : 0;
  return CONF_BANDS.find((b) => v >= b.min) || CONF_BANDS[CONF_BANDS.length - 1];
}

const CONF_ADVICE = {
  high: "The engine has a clear picture of this one. Answering the questions below will still sharpen it, but approving as-is is reasonable.",
  med: "Worth answering the questions below before you approve — at this confidence they are load-bearing, not optional polish.",
  low: "The engine is unsure what you want. Anything you leave blank here becomes a decision point that pauses the run later to ask you — answering now is what prevents that.",
};

function confidenceAdvice(c) {
  if (typeof c !== "number") return null;
  const band = confidenceBand(c);
  return el(
    "div",
    { class: `card card-pad conf-advice conf-${band.key}` },
    el("div", { class: "conf-advice-head" },
      el("span", { class: "conf-pct mono", text: `${Math.round(c * 100)}%` }),
      el("span", { class: "conf-band-label", text: band.label })),
    el("p", { class: "conf-advice-body", text: CONF_ADVICE[band.key] })
  );
}

// Per-question answer fields (operator: "what information it needs, needs to
// be more defined... to assist the user in knowing what to give"). Each
// question is answerable in place; blank means "let research / the plan
// decide". collect() composes Q/A pairs + the free-form note into the
// /ideate/confirm feedback string, so each answer travels WITH its question.
function buildQuestionsCard(brief, feas, onAnyInput) {
  const qs = dedupeQuestions([...(feas?.clarifications_needed || []), ...(brief?.ambiguities || [])]);
  if (!qs.length) return null;
  const pairs = qs.map((q) => ({
    q,
    input: el("input", {
      class: "input input-sm question-answer",
      placeholder: "Your answer — or leave blank and research/the plan will decide",
      onInput: onAnyInput,
    }),
  }));
  const extra = el("textarea", {
    class: "input feedback-box",
    rows: "2",
    placeholder: "Anything else the engine should know or do differently? (optional)",
    onInput: onAnyInput,
  });
  // §17.1007 — a flat list of eight gets the first two answered carefully, the
  // next two skimmed, and the rest abandoned — and a skipped question is
  // indistinguishable from a deliberate blank. `clarifications_needed` (the
  // engine's own explicit asks) already sorts ahead of `ambiguities` in the
  // dedupe above, so the first three ARE the highest-value three: show those,
  // and put the tail one click away instead of in the way.
  const LEAD = 3;
  const item = ({ q, input }) =>
    el("li", { class: "qa-item" }, el("div", { class: "qa-question", text: q }), input);
  const lead = pairs.slice(0, LEAD);
  const tail = pairs.slice(LEAD);
  const node = el(
    "div",
    { class: "card card-pad brief-block questions-card" },
    el("h3", { class: "brief-heading", text: `The engine needs your input — ${qs.length} open question${qs.length === 1 ? "" : "s"}` }),
    el("p", { class: "dim questions-hint", text: "Answer any of these in plain words. Blank ones are fine — research fills the gaps, or they become explicit decision points that pause the run and ask you." }),
    el("ol", { class: "questions-list qa-list" }, ...lead.map(item)),
    tail.length
      ? el(
          "details",
          { class: "brief-details questions-more" },
          el("summary", {}, `${tail.length} more question${tail.length === 1 ? "" : "s"} — lower impact, answer if you know`),
          el("ol", { class: "questions-list qa-list", start: String(LEAD + 1) }, ...tail.map(item))
        )
      : null,
    extra
  );
  const collect = () => {
    const answered = pairs
      .filter(({ input }) => input.value.trim())
      .map(({ q, input }) => `Q: ${q}\nA: ${input.value.trim()}`);
    const note = extra.value.trim();
    return [...answered, note].filter(Boolean).join("\n\n") || null;
  };
  // §17.1007 — `note()` exposes JUST the free-form box, so "Send back for
  // changes" can carry over a correction the operator already typed there
  // instead of making them write it twice. `collect()` (Q/A pairs + note) is
  // still what the approve path sends as feedback.
  const note = () => extra.value.trim();
  return { node, collect, note };
}

// ── List (no jobId) ──────────────────────────────────────────────────
function renderList(container) {
  let disposed = false;
  let timer = null;
  const outlet = el("div", { class: "picker-outlet" }, loading("Loading pending approvals…"));
  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el("div", {}, el("h1", { text: "Approval Gate" }), el("div", { class: "sub", text: "Jobs awaiting your review before research + planning" }))
    ),
    outlet
  );

  async function load() {
    try {
      const res = await api.get("/jobs", { query: { status: "awaiting_confirmation", limit: 100 } });
      if (disposed) return;
      const jobs = res.jobs || [];
      if (!jobs.length) {
        mount(outlet, emptyState({
          icon: "✓",
          title: "All caught up",
          body: "No jobs are waiting for a go/no-go. New ideas that need approval before running land here.",
          action: { label: "＋ New idea", href: "#/new" },
        }));
        return;
      }
      mount(
        outlet,
        el(
          "div",
          { class: "grid grid-3" },
          ...jobs.map((j) =>
            el(
              "a",
              { class: "card card-pad picker-card", href: `#/job/${j.id}` },
              el("div", { class: "row row-wrap" }, statusBadge(j.status), el("span", { class: "spacer" }), el("span", { class: "faint mono", text: shortId(j.id) })),
              el("div", { class: "work-title", text: j.title || "(untitled)" }),
              el("div", { class: "faint", text: timeAgo(j.updated_at || j.created_at) })
            )
          )
        )
      );
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, () => load()));
    }
  }

  load();
  timer = setInterval(load, 10000);

  return () => {
    disposed = true;
    if (timer) clearInterval(timer);
  };
}

// ── Detail (one job) ─────────────────────────────────────────────────
// §17.859 — embedded in the job hub's Overview tab while the job sits at (or
// before) the approval gate; the standalone #/approvals/:id route died with
// the hub. The gate is a moment, not a place.
export function renderApprovalDetail(container, jobId) {
  let disposed = false;
  let pollTimer = null;
  let busy = false;

  const outlet = el("div", { class: "approval-detail" }, loading("Loading job…"));
  mount(container, outlet);

  const progress = el("div", { class: "approval-progress hidden" });
  const approveBtn = el("button", { class: "btn btn-primary", text: "✓ Approve", onClick: () => approve() });
  // Built per-render by buildQuestionsCard; approve() collects the Q/A pairs.
  let qa = null;
  // §17.895 — the approve chain is the ONLY execution entry point that never
  // read the global Auto/Assist mode, so the sidebar toggle silently did
  // nothing here: approve always parked the operator in the plan editor with
  // no assist session, and the "idea → approve → assist" progression the
  // OWUI auto-chain used to provide simply did not exist in the SPA. The
  // label now states the WHOLE destination, so the button never under-promises.
  const approveTail = () =>
    isAssist() ? " — research, plan & start the walkthrough" : " — research & plan";
  const refreshApproveLabel = () => {
    approveBtn.textContent =
      (qa && qa.collect() ? "✓ Approve with answers" : "✓ Approve") + approveTail();
    // Auto-run is an AUTO-mode concept: in Assist mode the walkthrough IS the
    // run, so the checkbox would be a second, contradictory switch.
    autoRunLabel.classList.toggle("hidden", isAssist());
  };
  // §17.818 (plan 5.5) — one approve semantic across surfaces: approve always
  // means confirm → plan-ready; RUNNING is an explicit choice. This toggle
  // mirrors the OWUI auto-chain for operators who want approve→run in one
  // gesture. UI preference only (localStorage) — the server chain entries
  // (/ideate/confirm → /dag → /execute/all) are identical either way.
  const autoRun = el("input", { type: "checkbox" });
  autoRun.checked = localStorage.getItem("scaffold_auto_run") === "1";
  autoRun.addEventListener("change", () =>
    localStorage.setItem("scaffold_auto_run", autoRun.checked ? "1" : "0"));
  const autoRunLabel = el("label", { class: "row faint autorun-toggle" },
    autoRun, " Auto-run after approve");
  // §17.895 — flipping the sidebar toggle while the gate is open must not
  // leave the button describing the other mode's destination.
  const offExecMode = onExecModeChange(() => refreshApproveLabel());
  // §17.1007 — the gate used to offer approve or reject-and-cancel, and the
  // most common correct answer at a gate is neither: it is "not yet, change
  // this". With that option missing and the only alternative painted in the
  // error colour and named after a cancellation, loss aversion pushed
  // operators into approving briefs they had doubts about. Cancel stays — it
  // is just no longer the only way to say no, and no longer shouts.
  const rejectBtn = el("button", { class: "btn btn-ghost btn-quiet-danger", text: "✕ Cancel this job", onClick: () => reject() });

  const reviseNotes = el("textarea", {
    class: "input revise-notes",
    rows: "3",
    placeholder: "What should change? e.g. \u201cIt assumed Docker — this host runs LXC only\u201d or \u201cdrop the alerting scope, just metrics for now\u201d",
  });
  const reviseSend = el("button", { class: "btn btn-primary btn-sm", text: "↩ Send it back", onClick: () => revise() });
  const reviseBox = el(
    "div",
    { class: "card card-pad revise-box hidden" },
    el("div", { class: "revise-title", text: "Send this brief back for another pass" }),
    el("p", { class: "dim revise-hint", text: "The job keeps its id and history — it goes back through the same refinement it just came out of, with your correction folded in. Nothing is thrown away." }),
    reviseNotes,
    el("div", { class: "row revise-actions" },
      reviseSend,
      el("button", { class: "btn btn-ghost btn-sm", text: "Never mind", onClick: () => reviseBox.classList.add("hidden") }))
  );
  const reviseBtn = el("button", {
    class: "btn",
    text: "↩ Send back for changes",
    title: "Not ready to approve? Say what is wrong and the engine refines it again — the job is not cancelled.",
    onClick: () => {
      reviseBox.classList.remove("hidden");
      // Carry over anything already typed in the gate's free-form note: the
      // operator who wrote "this assumed Docker" there meant exactly this.
      const carried = qa && qa.note ? qa.note() : "";
      if (carried && !reviseNotes.value.trim()) reviseNotes.value = carried;
      reviseNotes.focus();
    },
  });

  let waitingShown = false; // dedupe re-renders while polling the waiting state

  // §17.1007 — elapsed-vs-expected for the refining wait. Lives outside the
  // render so the 4s poll can update it in place without rebuilding the panel
  // (which is what `waitingShown` exists to prevent).
  const waitMeta = el("div", { class: "wait-meta" });
  const REFINE_TYPICAL_MAX_MIN = 9;

  function renderWaitMeta(job) {
    const startedAt = Date.parse(job.created_at || job.updated_at || "");
    if (!startedAt) {
      mount(waitMeta, el("span", { class: "faint", text: "Usually 1–9 min. This page updates itself." }));
      return;
    }
    const mins = Math.max(0, Math.floor((Date.now() - startedAt) / 60000));
    const over = mins > REFINE_TYPICAL_MAX_MIN;
    mount(
      waitMeta,
      el("span", { class: "wait-elapsed mono", text: `${mins}m elapsed` }),
      el("span", { class: over ? "wait-expect warn" : "wait-expect faint",
        text: over
          // Past the window, keep being honest rather than repeating a promise
          // the run has already broken.
          ? `· longer than the usual 1–9 min. Still running — CPU inference times vary a lot with model and load.`
          : `· usually 1–9 min` }),
      el("span", { class: "faint", text: "· this page updates itself" })
    );
  }

  function startWaitPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(load, 4000);
  }
  function stopWaitPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function load() {
    try {
      const job = await api.get(`/jobs/${jobId}`);
      if (disposed) return;
      const st = job.status;

      if (st === "awaiting_confirmation") {
        stopWaitPoll();
        waitingShown = false;
        refreshApproveLabel(); // §17.895 — mode-correct label on every render
        const fg = flowGuide(job, { here: `#/job/${jobId}` });
        const brief = job.refined_brief || {};
        const feas = job.feasibility || {};
        // Verdict chips: the go/no-go signal belongs at the top, not inside
        // a JSON dump.
        const verdict =
          typeof feas.feasible === "boolean"
            ? el(
                "span",
                { class: feas.feasible ? "tag tag-ok" : "tag tag-err" },
                feas.feasible ? "✓ Feasible" : "✕ Not feasible",
                feas.confidence != null ? ` · ${confidenceBand(feas.confidence).label}` : ""
              )
            : null;
        // §17.1007 — the raw scalar told the operator nothing they could act
        // on: the same approve block was offered at 30% as at 95%. The band
        // names what to DO, which is the only form of a confidence number a
        // non-specialist can calibrate against.
        const advice = confidenceAdvice(feas.confidence);
        mount(
          outlet,
          fg,
          el(
            "div",
            { class: "row row-wrap" },
            statusBadge(st),
            el("h2", { class: "approval-title", text: job.title || "(untitled)" }),
            verdict,
            brief.complexity ? el("span", { class: "tag", text: `complexity: ${brief.complexity}` }) : null,
            job.deliverable_kind ? el("span", { class: "tag", text: job.deliverable_kind }) : null
          ),
          advice, // §17.1007 — what this confidence means for what you do next
          // 1. What the engine understood + its assessment — prose first.
          (brief.description || feas.summary)
            ? el(
                "div",
                { class: "card card-pad brief-block" },
                brief.description ? el("div", {}, el("h3", { class: "brief-heading", text: "What will be built" }), el("div", { class: "md brief-prose", html: mdToHtml(brief.description) })) : null,
                feas.summary ? el("div", { class: "assessment" }, el("h3", { class: "brief-heading", text: "Assessment" }), el("div", { class: "md brief-prose", html: mdToHtml(feas.summary) })) : null
              )
            : null,
          // 2. The engine's questions — the hero of this page: answering them
          // is how the operator steers toward the outcome they actually want.
          (qa = buildQuestionsCard(brief, feas, refreshApproveLabel))?.node ?? null,
          // 3. Decision controls directly after the questions — answer, approve,
          // no scrolling past the reference material to act.
          progress,
          // §17.1007 — three ways forward, weighted: approve leads, "send back"
          // is the ordinary second answer, cancel is the quiet last resort.
          el("div", { class: "drawer-actions approval-actions" }, approveBtn, reviseBtn, autoRunLabel, el("span", { class: "spacer" }), rejectBtn),
          reviseBox,
          // 4. Reference material last, collapsed with counts.
          el(
            "div",
            { class: "card card-pad brief-block" },
            el("h3", { class: "brief-heading", text: "Details" }),
            collapsible("Goals", (brief.goals || []).length, listOrNull(brief.goals)),
            collapsible("Expected outputs", (brief.outputs_expected || []).length, listOrNull(brief.outputs_expected)),
            collapsible("Constraints", (brief.constraints || []).length, listOrNull(brief.constraints)),
            collapsible("Inputs available", (brief.inputs_available || []).length, listOrNull(brief.inputs_available)),
            collapsible("Risks", (feas.risks || []).length, listOrNull(feas.risks)),
            collapsible("Planned research", (feas.recommended_research_queries || []).length, listOrNull(feas.recommended_research_queries)),
            job.input_text ? collapsible("Original request", null, el("div", { class: "md", html: mdToHtml(job.input_text) })) : null,
            collapsible("Other brief fields", null, renderRecord(brief, new Set(["description", "goals", "outputs_expected", "constraints", "inputs_available", "ambiguities", "complexity", "title", "domain"]))),
            collapsible("Other feasibility fields", null, renderRecord(feas, new Set(["summary", "feasible", "confidence", "risks", "clarifications_needed", "recommended_research_queries"])))
          )
        );
        return;
      }

      if (PRE_APPROVAL.has(st)) {
        // Feasibility not ready yet — live waiting state; keep polling until it
        // becomes approvable (or the operator cancels). Rendered once to avoid a
        // spinner flicker on every poll.
        if (!waitingShown) {
          mount(
            outlet,
            el("div", { class: "row" }, statusBadge(st), el("h2", { class: "approval-title", text: job.title || "(untitled)" })),
            el(
              "div",
              { class: "approval-progress" },
              el("span", { class: "spin" }),
              el("span", { class: "progress-msg", text: "Refining your idea — the engine is assessing feasibility and will list the questions it needs YOU to answer." })
            ),
            // §17.1007 — two things the bare spinner left the operator guessing
            // at. First, elapsed-vs-expected: naming "1–9 min" and then showing
            // nothing meant that at minute twelve the operator could not tell
            // whether they were inside the promise or past it, which silently
            // converts a bounded wait back into an unbounded one. Second, and
            // more important: this wait is SAFE TO LEAVE — refinement runs
            // server-side and the job persists. The theater's run is not, and
            // the console never said which was which, so operators learned to
            // sit and stare at both.
            waitMeta,
            el("p", { class: "dim wait-leave" }, "Safe to close this tab — the job keeps going, and it'll be here when you come back."),
            job.input_text ? el("div", { class: "card card-pad brief-block" }, el("h3", { class: "brief-heading", text: "Original request" }), el("div", { class: "md", html: mdToHtml(job.input_text) })) : null,
            el("div", { class: "drawer-actions" }, rejectBtn)
          );
          waitingShown = true;
        }
        renderWaitMeta(job);
        startWaitPoll();
        return;
      }

      // Past the approval gate already (researching/planning/executing/…).
      stopWaitPoll();
      waitingShown = false;
      mount(
        outlet,
        flowGuide(job),
        el(
          "div",
          { class: "card card-pad" },
          el("div", { class: "row" }, statusBadge(st), el("h2", { class: "approval-title", text: job.title || "(untitled)" })),
          el("p", { class: "dim", text: `This job has moved past the approval gate (status: ${st}).` }),
          // §17.843 — receipt: the answers as the SERVER received them.
          job.user_feedback
            ? el(
                "details",
                { class: "brief-details", open: "" },
                el("summary", {}, "✓ Your answers were received and folded into the research & plan"),
                el("pre", { class: "md-pre feedback-receipt", text: job.user_feedback })
              )
            : null,
          el(
            "div",
            { class: "drawer-actions" },
            (job.node_count || 0) > 0 ? el("a", { class: "btn btn-sm btn-primary", href: `#/job/${jobId}/plan`, text: "Open plan editor" }) : null,
            job.has_compiled_output ? el("a", { class: "btn btn-sm", href: `#/job/${jobId}/output`, text: "View output" }) : null
          )
        )
      );
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, () => load()));
    }
  }

  function setBusy(on) {
    busy = on;
    approveBtn.disabled = on;
    rejectBtn.disabled = on;
    reviseBtn.disabled = on; // §17.1007
  }

  function showProgress(msg) {
    progress.classList.remove("hidden");
    mount(progress, el("span", { class: "spin" }), el("span", { class: "progress-msg", text: msg }));
  }

  async function pollStatus() {
    try {
      const job = await api.get(`/jobs/${jobId}`);
      if (disposed) return;
      const nc = job.node_count || 0;
      const msg = nc > 0 ? `${job.status} · ${nc} nodes planned…` : `${job.status}…`;
      const line = progress.querySelector(".progress-msg");
      if (line) line.textContent = msg;
    } catch {
      /* transient — keep the last line */
    }
  }

  // §17.1036 — poll the server-owned chain until it is done (or errors).
  // Reloading this page mid-chain is fine: the chain keeps running and the
  // job page's own flow guide picks it up from status.
  async function waitForChain() {
    const t0 = Date.now();
    while (!disposed && Date.now() - t0 < 60 * 60 * 1000) {
      try {
        const st = await api.get(`/jobs/${jobId}/approve`);
        const phaseText = { research: "Researching & compiling…", planning: "Generating plan (DAG)…",
          assist: "Starting the guided walkthrough…", execute: "Starting execution…" }[st.phase];
        const line = progress.querySelector(".progress-msg");
        if (line && phaseText) line.textContent = phaseText;
        if (st.chain === "done" || st.chain === "error") return st;
        if (st.chain === "idle" && st.node_count > 0) return { chain: "done" };
      } catch {
        /* transient */
      }
      await new Promise((r) => setTimeout(r, 2500));
    }
    return null;
  }

  async function approve() {
    if (busy) return;
    setBusy(true);
    const fb = qa ? qa.collect() : null;
    const nAns = fb ? (fb.match(/^Q:/gm) || []).length : 0;
    const assist = isAssist(); // §17.895 — latch the mode for the whole chain
    showProgress(
      nAns
        ? `✓ ${nAns} answer${nAns === 1 ? "" : "s"} received — researching with your input… (a few minutes)`
        : "Researching & compiling… (this can take a few minutes)"
    );
    pollTimer = setInterval(pollStatus, 2500);
    try {
      // §17.1036 — the chain is the SERVER's: research → plan → (assist |
      // execute) runs detached and survives this page closing. Before this,
      // `await /ideate/confirm` then `await /dag` ran HERE, and a tab closed
      // between the two left the job stranded in `planning` with no plan.
      // Q/A pairs + free-form note from the questions card travel as feedback
      // and are folded into the brief before research.
      await api.post(`/jobs/${jobId}/approve`, {
        feedback: fb, assist, execute: !assist && autoRun.checked,
      });
      if (disposed) return;
      const state = await waitForChain();
      if (disposed) return;
      if (state && state.chain === "error") {
        toast(`The engine could not finish approving: ${state.error || "unknown error"}`, "err");
        router.navigate(`/job/${jobId}`);
        return;
      }
      if (assist) {
        // §17.895 — Assist mode carries STRAIGHT THROUGH into the walkthrough;
        // the server started the session as the chain's last phase.
        toast("Assist mode — the engine guides, you drive.", "ok");
        router.navigate(`/job/${jobId}/run`);
      } else if (autoRun.checked) {
        // §17.818 — hand off to the hub's Run tab (same /execute/all SSE
        // the manual Run uses; sessionStorage carries the one-shot intent).
        sessionStorage.setItem("scaffold_autorun", jobId);
        toast("Approved — plan generated. Starting execution…", "ok");
        router.navigate(`/job/${jobId}/run`);
      } else {
        toast("Approved — plan generated. Edit before executing.", "ok");
        router.navigate(`/job/${jobId}/plan`);
      }
    } catch (e) {
      if (!disposed) {
        // §17.846 — a failure mid-chain must leave a READABLE trail and then
        // reconcile with the server: confirm+plan can take minutes, and the
        // server frequently finishes fine even when the client-side chain
        // breaks (live incident: both calls returned 200, the operator only
        // saw a vanishing toast). The sticky toast holds the message; the
        // inline panel persists it on the page; load() re-syncs — if the job
        // actually advanced, the past-gate panel with "Open plan editor"
        // replaces the mystery.
        toast(`Approve failed: ${e.detail || e.message}`, "err");
        progress.classList.remove("hidden");
        mount(
          progress,
          el("span", { text: "⚠ " }),
          el("span", { class: "progress-msg", text: `The approve chain hit an error: ${e.detail || e.message}. Checking the job's actual state…` })
        );
        setBusy(false);
        // §17.854 (audit G4) — only REBUILD the panel (which discards the
        // operator's typed answers + note) when the job actually ADVANCED past
        // the gate — then the past-gate "Open plan editor" panel is what we want.
        // If it's still awaiting_confirmation (the flaky-but-recoverable case),
        // keep the filled-in inputs so the operator can just hit Approve again
        // instead of retyping everything on the worst step to lose state.
        let advanced = true;
        try {
          const job = await api.get(`/jobs/${jobId}`);
          advanced = job.status !== "awaiting_confirmation";
        } catch { /* status unknown → fall back to the reconcile rebuild */ }
        if (advanced) load();
      }
    } finally {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }
  }

  async function revise() {
    if (busy) return;
    const notes = reviseNotes.value.trim();
    if (!notes) {
      toast("Say what should change — the engine needs something to act on.", "err");
      reviseNotes.focus();
      return;
    }
    setBusy(true);
    reviseSend.disabled = true;
    showProgress("Sending it back — refining again with your notes… (usually 1–9 min)");
    try {
      await api.post("/ideate/revise", { job_id: jobId, notes });
      if (disposed) return;
      toast("Sent back — the engine is refining it again with your correction.", "ok");
      reviseBox.classList.add("hidden");
      reviseNotes.value = "";
      waitingShown = false; // force the wait panel to render over this gate
      await load();
    } catch (e) {
      if (!disposed) {
        toast(`Could not send it back: ${e.detail || e.message}`, "err");
        progress.classList.add("hidden");
      }
    } finally {
      if (!disposed) {
        setBusy(false);
        reviseSend.disabled = false;
      }
    }
  }

  async function reject() {
    if (busy) return;
    if (!confirm("Cancel this job? Its brief is preserved and it can be inspected later.")) return;
    setBusy(true);
    try {
      await api.post(`/jobs/${jobId}/cancel`, {});
      if (disposed) return;
      toast("Job cancelled.", "ok");
      router.navigate("/approvals");
    } catch (e) {
      if (!disposed) {
        toast(`Cancel failed: ${e.detail || e.message}`, "err");
        setBusy(false);
      }
    }
  }

  load();

  return () => {
    disposed = true;
    if (pollTimer) clearInterval(pollTimer);
    offExecMode(); // §17.895 — don't leak the exec-mode listener per render
  };
}

export default function approvals(container) {
  return renderList(container);
}
