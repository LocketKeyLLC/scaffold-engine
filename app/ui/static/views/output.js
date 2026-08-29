// Compiled output viewer. Renders a job's final deliverable (compiled_output)
// plus each node's output, with copy + download. Sources the deliverable from
// GET /logs/{id}?include_compiled=true&include_output=true and job metadata from
// GET /jobs/{id}.
import * as api from "../api.js";
import { el, mount, mdToHtml, copy } from "../util.js";
import { statusBadge, loading, errorPanel, toast, emptyState } from "../components.js";

function download(filename, textContent) {
  const blob = new Blob([textContent], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ── Detail ───────────────────────────────────────────────────────────
// §17.859 — embedded as the job hub's Output tab (picker + standalone route
// died with the hub; the hub header carries back/compare).
export function renderOutput(container, jobId) {
  let disposed = false;
  const outlet = el("div", { class: "output-view" }, loading("Loading output…"));
  mount(container, outlet);

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
        : emptyState({ icon: "∅", title: "No compiled output", body: "This job hasn't produced a deliverable yet. Individual node outputs are below." });

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
