// Interactive DAG canvas. Auto-layout (no stored x/y): longest-path layering
// from depends_on + barycenter ordering, rendered as pan/zoom SVG. Node click
// opens a detail drawer that lazy-loads the node's output + prompt.
import * as api from "../api.js";
import { el, mount, shortId, mdToHtml, timeAgo } from "../util.js";
import { statusBadge, loading, errorPanel } from "../components.js";

const SVGNS = "http://www.w3.org/2000/svg";
const NW = 194,
  NH = 60,
  HGAP = 76,
  VGAP = 28;

function svg(tag, attrs = {}, ...children) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) n.append(c);
  return n;
}

// ── Layout ────────────────────────────────────────────────────────────
function layout(nodes) {
  const byKey = Object.fromEntries(nodes.map((n) => [n.node_key, n]));
  const layer = {};
  const stack = new Set();
  function lay(key) {
    if (layer[key] != null) return layer[key];
    if (stack.has(key)) return 0; // cycle guard (server guarantees DAG)
    stack.add(key);
    const deps = (byKey[key].depends_on || []).filter((d) => byKey[d]);
    let m = 0;
    for (const d of deps) m = Math.max(m, lay(d) + 1);
    stack.delete(key);
    return (layer[key] = m);
  }
  nodes.forEach((n) => lay(n.node_key));

  const layers = {};
  nodes.forEach((n) => {
    (layers[layer[n.node_key]] ||= []).push(n.node_key);
  });
  const maxLayer = Math.max(0, ...Object.keys(layers).map(Number));

  const pos = {};
  const bary = (k) => {
    const deps = (byKey[k].depends_on || []).filter((d) => pos[d] != null);
    if (!deps.length) return byKey[k].execution_order ?? 0;
    return deps.reduce((a, d) => a + pos[d], 0) / deps.length;
  };
  for (let L = 0; L <= maxLayer; L++) {
    const keys = layers[L] || [];
    keys.sort((a, b) => {
      const d = (L === 0 ? (byKey[a].execution_order ?? 0) : bary(a)) - (L === 0 ? (byKey[b].execution_order ?? 0) : bary(b));
      return d !== 0 ? d : (byKey[a].execution_order ?? 0) - (byKey[b].execution_order ?? 0);
    });
    keys.forEach((k, i) => (pos[k] = i));
    layers[L] = keys;
  }

  const rowsMax = Math.max(1, ...Object.values(layers).map((k) => k.length));
  const coords = {};
  for (let L = 0; L <= maxLayer; L++) {
    const keys = layers[L];
    const offset = ((rowsMax - keys.length) * (NH + VGAP)) / 2; // vertical centering
    keys.forEach((k, i) => {
      coords[k] = { x: L * (NW + HGAP), y: offset + i * (NH + VGAP) };
    });
  }
  const width = (maxLayer + 1) * NW + maxLayer * HGAP;
  const height = rowsMax * NH + (rowsMax - 1) * VGAP;
  return { coords, byKey, width, height, layer, maxLayer };
}

function edgePath(a, b) {
  const x1 = a.x + NW,
    y1 = a.y + NH / 2,
    x2 = b.x,
    y2 = b.y + NH / 2;
  const dx = Math.max(30, (x2 - x1) / 2);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

// ── Canvas view for one job ──────────────────────────────────────────
function renderCanvas(container, jobId) {
  let disposed = false;
  let outputsCache = null; // {node_key: output_text}
  let selected = null;
  let LAYOUT = null;
  const view = { tx: 40, ty: 40, scale: 1 };

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
      el("button", { class: "btn btn-sm", text: "Fit", onClick: () => fit() }),
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

  let svgRoot, gRoot, nodeEls;

  function applyTransform() {
    if (gRoot) gRoot.setAttribute("transform", `translate(${view.tx},${view.ty}) scale(${view.scale})`);
  }

  function fit() {
    if (!svgRoot || !LAYOUT) return;
    const { width, height } = LAYOUT;
    const cw = canvas.clientWidth || 800,
      ch = canvas.clientHeight || 600;
    const pad = 60;
    const wS = (cw - pad) / Math.max(width, 1);
    const hS = (ch - pad) / Math.max(height, 1);
    // Wide/near-linear graphs: fill the height and let the user pan sideways,
    // rather than shrinking every node to a dot to fit the full width.
    const wide = width > height * 2.2;
    let s = wide ? Math.min(hS, 1.2) : Math.min(wS, hS, 1.2);
    view.scale = Math.max(0.18, s);
    const scaledW = width * view.scale;
    view.tx = scaledW <= cw - pad ? (cw - scaledW) / 2 : pad / 2;
    view.ty = (ch - height * view.scale) / 2;
    applyTransform();
  }

  function select(key, data) {
    selected = key;
    nodeEls?.forEach((g) => g.classList.toggle("sel", g.dataset.key === key));
    openDrawer(data.byKey[key]);
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
    nodeEls?.forEach((g) => g.classList.remove("sel"));
  }

  function draw(data) {
    LAYOUT = data;
    const { coords, byKey } = data;
    svgRoot = svg("svg", { class: "dag-svg", width: "100%", height: "100%" });
    // arrow marker
    const defs = svg("defs", {});
    const marker = svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
    marker.append(svg("path", { d: "M0,0 L10,5 L0,10 z", class: "edge-arrow" }));
    defs.append(marker);
    svgRoot.append(defs);

    gRoot = svg("g", {});
    svgRoot.append(gRoot);

    // edges first (under nodes)
    const gEdges = svg("g", { class: "edges" });
    for (const n of Object.values(byKey)) {
      for (const dep of n.depends_on || []) {
        if (!coords[dep]) continue;
        gEdges.append(svg("path", { d: edgePath(coords[dep], coords[n.node_key]), class: "edge", "marker-end": "url(#arrow)" }));
      }
    }
    gRoot.append(gEdges);

    // nodes
    nodeEls = [];
    const gNodes = svg("g", { class: "nodes" });
    for (const n of Object.values(byKey)) {
      const c = coords[n.node_key];
      const g = svg("g", { class: `node st-${n.status}`, transform: `translate(${c.x},${c.y})` });
      g.dataset.key = n.node_key;
      g.append(svg("rect", { class: "node-box", width: NW, height: NH, rx: 9 }));
      g.append(svg("rect", { class: "node-accent", width: 5, height: NH, rx: 2 }));
      g.append(svg("text", { class: "node-key", x: 14, y: 22, text: n.node_key }));
      g.append(svg("text", { class: "node-tool", x: NW - 12, y: 22, "text-anchor": "end", text: n.tool || "LLM" }));
      g.append(svg("text", { class: "node-title", x: 14, y: 42, text: truncate(n.title || "", 26) }));
      if (n.is_deliverable) g.append(svg("circle", { class: "node-star", cx: NW - 14, cy: 44, r: 4 }));
      g.addEventListener("click", () => select(n.node_key, data));
      gNodes.append(g);
      nodeEls.push(g);
    }
    gRoot.append(gNodes);

    mount(canvas, svgRoot);
    wirePanZoom();
    fit();
  }

  function truncate(s, n) {
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  function wirePanZoom() {
    let dragging = false,
      sx = 0,
      sy = 0,
      ox = 0,
      oy = 0;
    canvas.addEventListener("mousedown", (e) => {
      if (e.target.closest(".node")) return;
      dragging = true;
      sx = e.clientX;
      sy = e.clientY;
      ox = view.tx;
      oy = view.ty;
      canvas.classList.add("grabbing");
    });
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    function onMove(e) {
      if (!dragging) return;
      view.tx = ox + (e.clientX - sx);
      view.ty = oy + (e.clientY - sy);
      applyTransform();
    }
    function onUp() {
      dragging = false;
      canvas.classList.remove("grabbing");
    }
    canvas.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left,
          my = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
        const ns = Math.min(2.5, Math.max(0.12, view.scale * factor));
        // zoom around cursor
        view.tx = mx - ((mx - view.tx) * ns) / view.scale;
        view.ty = my - ((my - view.ty) * ns) / view.scale;
        view.scale = ns;
        applyTransform();
      },
      { passive: false }
    );
    canvas._cleanupPZ = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
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
      const lay = layout(data.nodes || []);
      draw(lay);
      if (selected && lay.byKey[selected]) select(selected, lay);
    } catch (e) {
      if (!disposed) mount(canvas, errorPanel(e, () => load()));
    }
  }

  load();

  return () => {
    disposed = true;
    if (canvas._cleanupPZ) canvas._cleanupPZ();
    LAYOUT = null;
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
        mount(outlet, el("div", { class: "card empty-state" }, el("div", { class: "empty-icon", text: "⬡" }), el("p", { text: "No jobs with a DAG yet." })));
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
