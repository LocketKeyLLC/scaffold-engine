// Shared DAG graph engine for the /ui canvas views — consumed by both the
// read-only dag.js and the editable plan.js. Auto-layout (no stored x/y):
// longest-path layering from depends_on + barycenter ordering, rendered as a
// pan/zoom SVG. The controller owns the SVG element, transform state, selection
// highlight, and pan/zoom wiring; each view supplies node-click behavior and its
// own drawer/toolbar. Node <g> markup is identical across both views, so the
// existing .dag-*/.node* CSS is reused unchanged (no visual regression).

const SVGNS = "http://www.w3.org/2000/svg";
export const DIMS = { NW: 194, NH: 60, HGAP: 76, VGAP: 28 };
const { NW, NH, HGAP, VGAP } = DIMS;

export function svg(tag, attrs = {}, ...children) {
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
export function layout(nodes) {
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

export function edgePath(a, b) {
  const x1 = a.x + NW,
    y1 = a.y + NH / 2,
    x2 = b.x,
    y2 = b.y + NH / 2;
  const dx = Math.max(30, (x2 - x1) / 2);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

function truncate(s, n) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// ── Stateful controller ──────────────────────────────────────────────
// createGraphCanvas(canvasEl) → { render, fit, setSelected, clearSelected,
//   onNodeClick, get layout, get selected, destroy }.
// A view instantiates one controller, wires onNodeClick to open its drawer,
// then calls render(nodes) on each (re)load.
export function createGraphCanvas(canvas) {
  let svgRoot, gRoot;
  let nodeEls = [];
  let LAYOUT = null;
  let selected = null;
  let clickHandler = null;
  const view = { tx: 40, ty: 40, scale: 1 };

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

  function setSelected(key) {
    selected = key;
    nodeEls.forEach((g) => g.classList.toggle("sel", g.dataset.key === key));
  }

  function clearSelected() {
    selected = null;
    nodeEls.forEach((g) => g.classList.remove("sel"));
  }

  function draw(data) {
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
      g.addEventListener("click", () => clickHandler && clickHandler(n.node_key, byKey[n.node_key]));
      gNodes.append(g);
      nodeEls.push(g);
    }
    gRoot.append(gNodes);

    canvas.replaceChildren(svgRoot);
    wirePanZoom();
    fit();
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
    // Idempotent: a re-render replaces the SVG but re-runs wirePanZoom, so
    // detach the prior window listeners before the new ones supersede them.
    if (canvas._cleanupPZ) canvas._cleanupPZ();
    canvas._cleanupPZ = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }

  return {
    render(nodes) {
      LAYOUT = layout(nodes || []);
      draw(LAYOUT);
      if (selected && LAYOUT.byKey[selected]) setSelected(selected);
      return LAYOUT;
    },
    fit,
    setSelected,
    clearSelected,
    onNodeClick(fn) {
      clickHandler = fn;
    },
    get layout() {
      return LAYOUT;
    },
    get selected() {
      return selected;
    },
    destroy() {
      if (canvas._cleanupPZ) canvas._cleanupPZ();
      LAYOUT = null;
      nodeEls = [];
    },
  };
}
