// Approval gate. Lists jobs halted at `awaiting_confirmation` and renders the
// Phase-1 refined brief + feasibility for review. Approve runs the two-stage
// chain (POST /ideate/confirm → POST /dag, progress polled) then lands in the
// plan editor with nothing executed; Reject cancels the job (non-destructive).
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount, shortId, timeAgo, mdToHtml } from "../util.js";
import { statusBadge, loading, errorPanel, toast } from "../components.js";

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

function renderRecord(obj) {
  if (!obj || typeof obj !== "object" || !Object.keys(obj).length) {
    return el("div", { class: "dim", text: "Not available." });
  }
  return el(
    "div",
    { class: "brief-record" },
    ...Object.entries(obj).map(([k, v]) =>
      el(
        "div",
        { class: "brief-field" },
        el("div", { class: "brief-key", text: k.replace(/_/g, " ") }),
        el("div", { class: "brief-val" }, renderValue(v))
      )
    )
  );
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
        mount(outlet, el("div", { class: "card empty-state" }, el("div", { class: "empty-icon", text: "✓" }), el("p", { text: "Nothing awaiting approval." })));
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
  const rejectBtn = el("button", { class: "btn btn-danger", text: "✕ Reject (cancel)", onClick: () => reject() });

  async function load() {
    try {
      const job = await api.get(`/jobs/${jobId}`);
      if (disposed) return;
      if (job.status !== "awaiting_confirmation") {
        // Already moved on — show status and route the operator onward.
        mount(
          outlet,
          el(
            "div",
            { class: "card card-pad" },
            el("div", { class: "row" }, statusBadge(job.status), el("h2", { class: "approval-title", text: job.title || "(untitled)" })),
            el("p", { class: "dim", text: `This job is no longer awaiting approval (status: ${job.status}).` }),
            el(
              "div",
              { class: "drawer-actions" },
              (job.node_count || 0) > 0 ? el("a", { class: "btn btn-sm btn-primary", href: `#/plan/${jobId}`, text: "Open plan editor" }) : null,
              job.has_compiled_output ? el("a", { class: "btn btn-sm", href: `#/output/${jobId}`, text: "View output" }) : null,
              el("a", { class: "btn btn-sm btn-ghost", href: `#/dag/${jobId}`, text: "View DAG" })
            )
          )
        );
        return;
      }
      mount(
        outlet,
        el("div", { class: "row" }, statusBadge(job.status), el("h2", { class: "approval-title", text: job.title || "(untitled)" }), job.deliverable_kind ? el("span", { class: "tag", text: job.deliverable_kind }) : null),
        job.input_text ? el("div", { class: "card card-pad brief-block" }, el("h3", { class: "brief-heading", text: "Original request" }), el("div", { class: "md", html: mdToHtml(job.input_text) })) : null,
        el("div", { class: "card card-pad brief-block" }, el("h3", { class: "brief-heading", text: "Refined brief" }), renderRecord(job.refined_brief)),
        el("div", { class: "card card-pad brief-block" }, el("h3", { class: "brief-heading", text: "Feasibility" }), renderRecord(job.feasibility)),
        progress,
        el("div", { class: "drawer-actions approval-actions" }, approveBtn, rejectBtn)
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
    showProgress("Researching & compiling… (this can take a few minutes)");
    pollTimer = setInterval(pollStatus, 2500);
    try {
      // Phase 2: research → ingest → compile → planning. Synchronous, minutes.
      await api.post("/ideate/confirm", { job_id: jobId });
      if (disposed) return;
      const line = progress.querySelector(".progress-msg");
      if (line) line.textContent = "Generating plan (DAG)…";
      // Generate the DAG but DO NOT execute — the operator edits it next.
      await api.post("/dag", { job_id: jobId });
      if (disposed) return;
      toast("Approved — plan generated. Edit before executing.", "ok");
      router.navigate(`/plan/${jobId}`);
    } catch (e) {
      if (!disposed) {
        toast(`Approve failed: ${e.detail || e.message}`, "err");
        progress.classList.add("hidden");
        setBusy(false);
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
