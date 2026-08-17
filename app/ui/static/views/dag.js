// Interactive DAG canvas (read-only). Auto-layout + pan/zoom SVG live in the
// shared dag_render.js controller; this view supplies the header, legend strip,
// and a read-only node drawer that lazy-loads the node's output + prompt.
import * as api from "../api.js";
import { el, mount, shortId, mdToHtml, timeAgo } from "../util.js";
import { statusBadge, loading, errorPanel, emptyState } from "../components.js";
import { createGraphCanvas } from "./dag_render.js";

// ── Canvas view for one job ──────────────────────────────────────────
function renderCanvas(container, jobId) {
  let disposed = false;
  let outputsCache = null; // {node_key: output_text}
  let selected = null;

  const header = el(
    "div",
    { class: "view-header" },
    el(
      "div",
      {},
      el("h1", { text: "DAG Canvas" }),
      el("div", { class: "sub mono", text: shortId(jobId) })
    ),
    el(
      "div",
      { class: "header-actions" },
      el("a", { class: "btn btn-sm btn-ghost", href: "#/dag", text: "← Jobs" }),
      el("button", { class: "btn btn-sm", text: "Fit", onClick: () => graph.fit() }),
      el("a", { class: "btn btn-sm btn-primary", href: `#/theater/${jobId}`, text: "▶ Execution" }),
      el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => load() })
    )
  );

  const legendBar = el("div", { class: "dag-legendbar" });
  const canvas = el("div", { class: "dag-canvas" });
  const drawer = el("div", { class: "dag-drawer hidden" });
  const stage = el("div", { class: "dag-stage" }, canvas, drawer);
  mount(container, header, legendBar, stage);
  mount(canvas, loading("Loading DAG…"));

  const graph = createGraphCanvas(canvas);
  graph.onNodeClick((key) => select(key));

  function select(key) {
    const node = graph.layout && graph.layout.byKey[key];
    if (!node) return;
    selected = key;
    graph.setSelected(key);
    openDrawer(node);
  }

  async function openDrawer(node) {
    drawer.classList.remove("hidden");
    const body = el("div", { class: "drawer-body" }, loading("Loading node…"));
    mount(
      drawer,
      el(
        "div",
        { class: "drawer-head" },
        el("div", { class: "row" }, statusBadge(node.status), el("span", { class: "tag", text: node.tool || "LLM" }), node.is_deliverable ? el("span", { class: "tag deliverable", text: "deliverable" }) : null),
        el("button", { class: "btn btn-sm btn-ghost drawer-close", text: "✕", onClick: () => closeDrawer() })
      ),
      el("h3", { class: "drawer-title", text: `${node.node_key} · ${node.title || ""}` }),
      body
    );

    // meta grid
    const meta = el(
      "div",
      { class: "drawer-meta" },
      metaRow("Order", String(node.execution_order ?? "—")),
      metaRow("Depends on", (node.depends_on || []).join(", ") || "—"),
      metaRow("Model", node.assigned_model || "—"),
      metaRow("Confidence", node.confidence != null ? node.confidence.toFixed(2) : "—"),
      node.failure_reason ? metaRow("Failure", node.failure_reason, "err") : null
    );

    // lazy-load output text
    let outputText = "";
    try {
      if (!outputsCache) {
        const res = await api.get(`/exec/nodes/${jobId}`);
        outputsCache = Object.fromEntries((res.nodes || []).map((n) => [n.node_key, n.output_text || ""]));
      }
      outputText = outputsCache[node.node_key] || "";
    } catch {
      outputText = "";
    }
    if (disposed) return;

    const outBlock = outputText
      ? el("div", { class: "drawer-section" }, el("div", { class: "drawer-label", text: "Output" }), el("div", { class: "md drawer-output", html: mdToHtml(outputText) }))
      : el("div", { class: "drawer-section" }, el("div", { class: "drawer-label", text: "Output" }), el("div", { class: "dim", text: "No output yet." }));

    mount(body, meta, outBlock);
  }

  function metaRow(k, v, cls) {
    return el("div", { class: "meta-row" }, el("span", { class: "meta-k", text: k }), el("span", { class: `meta-v ${cls || ""}`, text: v }));
  }

  function closeDrawer() {
    drawer.classList.add("hidden");
    selected = null;
    graph.clearSelected();
  }

  async function load() {
    try {
      const data = await api.get(`/exec/status/${jobId}`);
      if (disposed) return;
      outputsCache = null;
      // legend / counts strip
      const counts = data.counts || {};
      mount(
        legendBar,
        el("span", { class: "faint mono", text: `${data.total_nodes ?? (data.nodes || []).length} nodes` }),
        ...Object.entries(counts).map(([k, v]) => el("span", { class: "strip-item" }, statusBadge(k), el("span", { class: "strip-n mono", text: String(v) })))
      );
      header.querySelector(".sub").textContent = data.job_title || shortId(jobId);
      graph.render(data.nodes || []);
      if (selected && graph.layout.byKey[selected]) select(selected);
    } catch (e) {
      if (!disposed) mount(canvas, errorPanel(e, () => load()));
    }
  }

  load();

  return () => {
    disposed = true;
    graph.destroy();
  };
}

// ── Job picker (no jobId in route) ───────────────────────────────────
function renderPicker(container) {
  let disposed = false;
  const outlet = el("div", { class: "picker-outlet" }, loading("Loading jobs…"));
  mount(
    container,
    el("div", { class: "view-header" }, el("div", {}, el("h1", { text: "DAG Canvas" }), el("div", { class: "sub", text: "Pick a job to inspect its graph" }))),
    outlet
  );

  (async () => {
    try {
      const res = await api.get("/jobs", { query: { limit: 100 } });
      if (disposed) return;
      const jobs = (res.jobs || []).filter((j) => (j.node_count || 0) > 0);
      if (!jobs.length) {
        mount(outlet, emptyState({
          icon: "⬡",
          title: "No DAGs yet",
          body: "Approve a plan for a job and its dependency graph appears here, ready to inspect and pan.",
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
              { class: "card card-pad picker-card", href: `#/dag/${j.id}` },
              el("div", { class: "row row-wrap" }, statusBadge(j.status), el("span", { class: "spacer" }), el("span", { class: "faint mono", text: `${j.node_count} nodes` })),
              el("div", { class: "work-title", text: j.title || "(untitled)" }),
              el("div", { class: "faint", text: timeAgo(j.updated_at || j.created_at) })
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

export default function dag(container, params) {
  if (params && params.jobId) return renderCanvas(container, params.jobId);
  return renderPicker(container);
}
