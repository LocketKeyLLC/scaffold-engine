// Approval gate. Lists jobs halted at `awaiting_confirmation` and renders the
// Phase-1 refined brief + feasibility for review. Approve runs the two-stage
// chain (POST /ideate/confirm → POST /dag, progress polled) then lands in the
// plan editor with nothing executed; Reject cancels the job (non-destructive).
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount, shortId, timeAgo, mdToHtml } from "../util.js";
import { statusBadge, loading, errorPanel, toast, emptyState } from "../components.js";

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
  const node = el(
    "div",
    { class: "card card-pad brief-block questions-card" },
    el("h3", { class: "brief-heading", text: `The engine needs your input — ${qs.length} open question${qs.length === 1 ? "" : "s"}` }),
    el("p", { class: "dim questions-hint", text: "Answer any of these in plain words. Blank ones are fine — research fills the gaps, or they become explicit decision points that pause the run and ask you." }),
    el(
      "ol",
      { class: "questions-list qa-list" },
      ...pairs.map(({ q, input }) => el("li", { class: "qa-item" }, el("div", { class: "qa-question", text: q }), input))
    ),
    extra
  );
  const collect = () => {
    const answered = pairs
      .filter(({ input }) => input.value.trim())
      .map(({ q, input }) => `Q: ${q}\nA: ${input.value.trim()}`);
    const note = extra.value.trim();
    return [...answered, note].filter(Boolean).join("\n\n") || null;
  };
  return { node, collect };
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
              { class: "card card-pad picker-card", href: `#/approvals/${j.id}` },
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
function renderDetail(container, jobId) {
  let disposed = false;
  let pollTimer = null;
  let busy = false;

  const outlet = el("div", { class: "approval-detail" }, loading("Loading job…"));
  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el("div", {}, el("h1", { text: "Approval Gate" }), el("div", { class: "sub mono", text: shortId(jobId) })),
      el("div", { class: "header-actions" }, el("a", { class: "btn btn-sm btn-ghost", href: "#/approvals", text: "← Pending" }))
    ),
    outlet
  );

  const progress = el("div", { class: "approval-progress hidden" });
  const approveBtn = el("button", { class: "btn btn-primary", text: "✓ Approve — research & plan", onClick: () => approve() });
  // Built per-render by buildQuestionsCard; approve() collects the Q/A pairs.
  let qa = null;
  const refreshApproveLabel = () => {
    approveBtn.textContent = qa && qa.collect()
      ? "✓ Approve with answers — research & plan"
      : "✓ Approve — research & plan";
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
  const rejectBtn = el("button", { class: "btn btn-danger", text: "✕ Reject (cancel)", onClick: () => reject() });

  let waitingShown = false; // dedupe re-renders while polling the waiting state

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
                feas.confidence != null ? ` · ${Math.round(feas.confidence * 100)}% confidence` : ""
              )
            : null;
        mount(
          outlet,
          el(
            "div",
            { class: "row row-wrap" },
            statusBadge(st),
            el("h2", { class: "approval-title", text: job.title || "(untitled)" }),
            verdict,
            brief.complexity ? el("span", { class: "tag", text: `complexity: ${brief.complexity}` }) : null,
            job.deliverable_kind ? el("span", { class: "tag", text: job.deliverable_kind }) : null
          ),
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
          el("div", { class: "drawer-actions approval-actions" }, approveBtn, rejectBtn, autoRunLabel),
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
              el("span", { class: "progress-msg", text: "Refining your idea — the engine is assessing feasibility and will list the questions it needs YOU to answer. Usually 1–9 min; this page updates itself." })
            ),
            job.input_text ? el("div", { class: "card card-pad brief-block" }, el("h3", { class: "brief-heading", text: "Original request" }), el("div", { class: "md", html: mdToHtml(job.input_text) })) : null,
            el("div", { class: "drawer-actions" }, rejectBtn)
          );
          waitingShown = true;
        }
        startWaitPoll();
        return;
      }

      // Past the approval gate already (researching/planning/executing/…).
      stopWaitPoll();
      waitingShown = false;
      mount(
        outlet,
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
            (job.node_count || 0) > 0 ? el("a", { class: "btn btn-sm btn-primary", href: `#/plan/${jobId}`, text: "Open plan editor" }) : null,
            job.has_compiled_output ? el("a", { class: "btn btn-sm", href: `#/output/${jobId}`, text: "View output" }) : null,
            el("a", { class: "btn btn-sm btn-ghost", href: `#/dag/${jobId}`, text: "View DAG" })
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

  async function approve() {
    if (busy) return;
    setBusy(true);
    const fb = qa ? qa.collect() : null;
    const nAns = fb ? (fb.match(/^Q:/gm) || []).length : 0;
    showProgress(
      nAns
        ? `✓ ${nAns} answer${nAns === 1 ? "" : "s"} received — researching with your input… (a few minutes)`
        : "Researching & compiling… (this can take a few minutes)"
    );
    pollTimer = setInterval(pollStatus, 2500);
    try {
      // Phase 2: research → ingest → compile → planning. Synchronous, minutes.
      // Q/A pairs + free-form note from the questions card travel as feedback
      // and are folded into the brief before research.
      await api.post("/ideate/confirm", { job_id: jobId, feedback: fb });
      if (disposed) return;
      const line = progress.querySelector(".progress-msg");
      if (line) line.textContent = "Generating plan (DAG)…";
      // Generate the DAG but DO NOT execute — the operator edits it next.
      await api.post("/dag", { job_id: jobId });
      if (disposed) return;
      if (autoRun.checked) {
        // §17.818 — hand off to the theater's runner (same /execute/all SSE
        // the manual Run uses; sessionStorage carries the one-shot intent).
        sessionStorage.setItem("scaffold_autorun", jobId);
        toast("Approved — plan generated. Starting execution…", "ok");
        router.navigate(`/theater/${jobId}`);
      } else {
        toast("Approved — plan generated. Edit before executing.", "ok");
        router.navigate(`/plan/${jobId}`);
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
        load();
      }
    } finally {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
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
  };
}

export default function approvals(container, params) {
  if (params && params.jobId) return renderDetail(container, params.jobId);
  return renderList(container);
}
