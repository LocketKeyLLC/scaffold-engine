// Compiled output viewer. Renders a job's final deliverable (compiled_output)
// plus each node's output, with copy + download. Sources the deliverable from
// GET /logs/{id}?include_compiled=true&include_output=true and job metadata from
// GET /jobs/{id}.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, mdToHtml, copy } from "../util.js";
import { statusBadge, loading, errorPanel, toast } from "../components.js";

function download(filename, textContent) {
  const blob = new Blob([textContent], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ── Picker ───────────────────────────────────────────────────────────
function renderPicker(container) {
  let disposed = false;
  const outlet = el("div", { class: "picker-outlet" }, loading("Loading completed jobs…"));
  mount(
    container,
    el("div", { class: "view-header" }, el("div", {}, el("h1", { text: "Outputs" }), el("div", { class: "sub", text: "Pick a completed job to view its deliverable" }))),
    outlet
  );

  (async () => {
    try {
      const res = await api.get("/jobs", { query: { status: "completed", limit: 100 } });
      if (disposed) return;
      const jobs = res.jobs || [];
      if (!jobs.length) {
        mount(outlet, el("div", { class: "card empty-state" }, el("div", { class: "empty-icon", text: "▤" }), el("p", { text: "No completed jobs yet." })));
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
              { class: "card card-pad picker-card", href: `#/output/${j.id}` },
              el("div", { class: "row row-wrap" }, statusBadge(j.status), el("span", { class: "spacer" }), el("span", { class: "faint mono", text: `${j.node_count || 0} nodes` })),
              el("div", { class: "work-title", text: j.title || "(untitled)" }),
              el("div", { class: "faint", text: timeAgo(j.completed_at || j.updated_at) })
            )
          )
        )
      );
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e));
    }
  })();

  return () => {
    disposed = true;
  };
}

// ── Detail ───────────────────────────────────────────────────────────
function renderOutput(container, jobId) {
  let disposed = false;
  const outlet = el("div", { class: "output-view" }, loading("Loading output…"));
  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el("div", {}, el("h1", { text: "Output" }), el("div", { class: "sub mono", text: shortId(jobId) })),
      el(
        "div",
        { class: "header-actions" },
        el("a", { class: "btn btn-sm btn-ghost", href: "#/output", text: "← Outputs" }),
        el("a", { class: "btn btn-sm", href: `#/dag/${jobId}`, text: "DAG" }),
        el("a", { class: "btn btn-sm", href: `#/compare/${jobId}`, text: "Compare" })
      )
    ),
    outlet
  );

  (async () => {
    try {
      const [job, logs] = await Promise.all([
        api.get(`/jobs/${jobId}`),
        api.get(`/logs/${jobId}`, { query: { include_compiled: true, include_output: true } }),
      ]);
      if (disposed) return;
      const compiled = logs.compiled_output || "";
      const kind = job.deliverable_kind || logs.deliverable_kind;

      const copyBtn = el("button", {
        class: "btn btn-sm",
        text: "Copy",
        onClick: async () => toast((await copy(compiled)) ? "Copied." : "Copy failed.", ""),
      });
      const dlBtn = el("button", {
        class: "btn btn-sm",
        text: "Download",
        onClick: () => download(`${(job.title || jobId).replace(/[^\w.-]+/g, "_").slice(0, 60)}.md`, compiled),
      });

      const compiledBlock = compiled
        ? el("div", { class: "card card-pad output-doc" }, el("div", { class: "md", html: mdToHtml(compiled) }))
        : el("div", { class: "card empty-state" }, el("div", { class: "empty-icon", text: "∅" }), el("p", { text: "No compiled output for this job." }));

      const nodeBlocks = (logs.nodes || []).map((n) =>
        el(
          "details",
          { class: "node-details" },
          el(
            "summary",
            {},
            el("span", { class: "mono", text: n.node_key }),
            el("span", { class: "node-details-title", text: n.title || "" }),
            statusBadge(n.status)
          ),
          n.output_preview
            ? el("div", { class: "md node-details-body", html: mdToHtml(n.output_preview) })
            : el("div", { class: "dim node-details-body", text: "No output." })
        )
      );

      mount(
        outlet,
        el(
          "div",
          { class: "row row-wrap output-head" },
          statusBadge(job.status),
          el("h2", { class: "output-title", text: job.title || "(untitled)" }),
          kind ? el("span", { class: "tag", text: kind }) : null,
          el("span", { class: "spacer" }),
          copyBtn,
          dlBtn
        ),
        compiledBlock,
        nodeBlocks.length ? el("h3", { class: "output-section-h", text: `Node outputs (${nodeBlocks.length})` }) : null,
        ...nodeBlocks
      );
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, () => renderOutput(container, jobId)));
    }
  })();

  return () => {
    disposed = true;
  };
}

export default function output(container, params) {
  if (params && params.jobId) return renderOutput(container, params.jobId);
  return renderPicker(container);
}
