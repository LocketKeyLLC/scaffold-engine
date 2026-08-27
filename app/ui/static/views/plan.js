// Plan editor — review + edit a generated DAG before execution. Reuses the
// shared graph controller (dag_render.js); the node drawer becomes an edit form
// wired to the node CRUD endpoints (PATCH/POST/DELETE/reorder/reset), with
// optimistic-lock (edit_version) 409 handling. "Execute plan" hands off to the
// execution theater. Reached via the approval gate's Approve chain.
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount, moveItem, shortId } from "../util.js";
import { statusBadge, loading, errorPanel, toast } from "../components.js";
import { createGraphCanvas } from "./dag_render.js";

// §17.815 — edit attribution is SERVER-derived from the API key (audit-trail
// integrity); the client no longer sends a spoofable label.

function field(label, control, hint) {
  return el(
    "div",
    { class: "node-field" },
    el("label", { class: "node-field-label", text: label }),
    control,
    hint ? el("div", { class: "node-field-hint", text: hint }) : null
  );
}

function renderPlan(container, jobId) {
  let disposed = false;
  let nodes = [];
  let byKey = {};
  let jobStatus = null;
  let selectedKey = null;

  const warning = el("div", { class: "plan-warning hidden" });
  const insertBtn = el("button", { class: "btn btn-sm", text: "＋ Insert node", onClick: () => openInsertDrawer() });
  const reorderBtn = el("button", { class: "btn btn-sm", text: "↕ Reorder", onClick: () => toggleReorder() });
  const executeBtn = el("a", { class: "btn btn-sm btn-primary", href: `#/theater/${jobId}`, text: "▶ Execute plan" });

  const header = el(
    "div",
    { class: "view-header" },
    el("div", {}, el("h1", { text: "Plan Editor" }), el("div", { class: "sub mono", text: shortId(jobId) })),
    el(
      "div",
      { class: "header-actions" },
      el("a", { class: "btn btn-sm btn-ghost", href: "#/approvals", text: "← Approvals" }),
      el("button", { class: "btn btn-sm", text: "Fit", onClick: () => graph.fit() }),
      el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => load() }),
      insertBtn,
      reorderBtn,
      executeBtn
    )
  );

  const canvas = el("div", { class: "dag-canvas" });
  const drawer = el("div", { class: "dag-drawer hidden" });
  const reorderPanel = el("div", { class: "reorder-panel hidden" });
  const stage = el("div", { class: "dag-stage" }, canvas, drawer);
  // Shown only at ≤820px (CSS): plan editing is a desktop-first surface.
  const mobileNote = el(
    "div",
    { class: "mobile-note" },
    "✎ Editing the plan is easiest on a wider screen. You can still tap a node to review it."
  );
  // Where-am-I guidance (operator critique: "no real guidance — are the
  // nodes researched and just awaiting confirmation?"). States the contract
  // plainly: research is done, this graph is a PROPOSAL, nothing runs yet.
  const guidance = el(
    "div",
    { class: "card card-pad plan-guidance" },
    el("p", {
      text: "Research is done and this plan was drawn from your brief plus what it found. Nothing has run yet — every node is a proposed step, executed in dependency order only after you press Execute plan.",
    }),
    el("p", {
      class: "dim",
      text: "Click a node to inspect or edit what it will do (instructions, tool, model, dependencies) · drag nothing — order comes from the arrows · Insert adds a step · decision nodes pause execution to ask you.",
    })
  );
  mount(container, header, mobileNote, guidance, warning, reorderPanel, stage);
  mount(canvas, loading("Loading plan…"));

  const graph = createGraphCanvas(canvas);
  graph.onNodeClick((key) => openEditDrawer(byKey[key]));

  function closeDrawer() {
    drawer.classList.add("hidden");
    selectedKey = null;
    graph.clearSelected();
  }

  // ── Load ─────────────────────────────────────────────────────────
  async function load() {
    try {
      const res = await api.get(`/nodes/${jobId}`);
      if (disposed) return;
      nodes = res.nodes || [];
      byKey = Object.fromEntries(nodes.map((n) => [n.node_key, n]));
      jobStatus = res.job_status;
      const ran = nodes.filter((n) => n.status !== "pending");
      if (ran.length) {
        warning.classList.remove("hidden");
        mount(
          warning,
          el("span", { class: "warn-icon", text: "⚠" }),
          el("span", { text: `${ran.length} node(s) have already run. Editing prompts, tools, or dependencies will reset them and everything downstream.` })
        );
      } else {
        warning.classList.add("hidden");
      }
      graph.render(nodes);
      if (selectedKey && byKey[selectedKey]) {
        graph.setSelected(selectedKey);
        openEditDrawer(byKey[selectedKey]);
      }
    } catch (e) {
      if (!disposed) mount(canvas, errorPanel(e, () => load()));
    }
  }

  // ── Edit drawer ──────────────────────────────────────────────────
  function depsSelect(current, excludeKey) {
    const sel = el("select", { class: "input node-multiselect", multiple: true, size: Math.min(6, Math.max(2, nodes.length - 1)) });
    for (const n of nodes) {
      if (n.node_key === excludeKey) continue;
      const opt = el("option", { value: n.node_key, text: `${n.node_key} · ${n.title || ""}` });
      if ((current || []).includes(n.node_key)) opt.selected = true;
      sel.append(opt);
    }
    return sel;
  }
  const readDeps = (sel) => Array.from(sel.selectedOptions).map((o) => o.value);

  function openEditDrawer(node) {
    if (!node) return;
    selectedKey = node.node_key;
    graph.setSelected(node.node_key);
    drawer.classList.remove("hidden");

    const titleIn = el("input", { class: "input", value: node.title || "" });
    const descIn = el("textarea", { class: "input node-textarea", rows: 2 }, node.description || "");
    const promptIn = el("textarea", { class: "input node-textarea", rows: 6 }, node.prompt_template || "");
    const toolIn = el("input", { class: "input", value: node.tool || "LLM" });
    const modelIn = el("input", { class: "input", value: node.assigned_model || "", placeholder: "(role default)" });
    const delivIn = el("input", { type: "checkbox" });
    if (node.is_deliverable) delivIn.checked = true;
    const depsIn = depsSelect(node.depends_on, node.node_key);
    const cfgIn = el("textarea", { class: "input node-textarea mono", rows: 3 }, node.tool_config ? JSON.stringify(node.tool_config, null, 2) : "");

    const saveBtn = el("button", { class: "btn btn-sm btn-primary", text: "Save", onClick: () => save() });
    const resetBtn = el("button", { class: "btn btn-sm", text: "Reset node", onClick: () => resetNode(node.node_key) });
    const deleteBtn = el("button", { class: "btn btn-sm btn-danger", text: "Delete", onClick: () => deleteNode(node.node_key) });

    mount(
      drawer,
      el(
        "div",
        { class: "drawer-head" },
        el("div", { class: "row" }, statusBadge(node.status), el("span", { class: "tag", text: `v${node.edit_version}` })),
        el("button", { class: "btn btn-sm btn-ghost drawer-close", text: "✕", onClick: () => closeDrawer() })
      ),
      el("h3", { class: "drawer-title", text: `${node.node_key} · edit` }),
      el(
        "div",
        { class: "node-form" },
        field("Title", titleIn),
        field("Description", descIn),
        field("Prompt template", promptIn, "Edited on an already-run node → resets it + downstream."),
        field("Tool", toolIn),
        field("Assigned model", modelIn),
        field("Depends on", depsIn),
        el("div", { class: "node-field row" }, delivIn, el("label", { class: "node-field-label inline", text: "Is deliverable" })),
        field("Tool config (JSON)", cfgIn, "MCP nodes only. Leave blank for none.")
      ),
      el("div", { class: "drawer-actions" }, saveBtn, resetBtn, deleteBtn)
    );

    async function save() {
      const fields = {};
      if (titleIn.value !== (node.title || "")) fields.title = titleIn.value;
      if (descIn.value !== (node.description || "")) fields.description = descIn.value;
      if (promptIn.value !== (node.prompt_template || "")) fields.prompt_template = promptIn.value;
      if (toolIn.value !== (node.tool || "LLM")) fields.tool = toolIn.value;
      if (modelIn.value !== (node.assigned_model || "")) fields.assigned_model = modelIn.value || null;
      if (delivIn.checked !== !!node.is_deliverable) fields.is_deliverable = delivIn.checked;
      const newDeps = readDeps(depsIn);
      if (JSON.stringify(newDeps) !== JSON.stringify(node.depends_on || [])) fields.depends_on = newDeps;
      // tool_config: parse JSON if changed
      const cfgRaw = cfgIn.value.trim();
      const origCfg = node.tool_config ? JSON.stringify(node.tool_config, null, 2) : "";
      if (cfgIn.value !== origCfg) {
        if (!cfgRaw) {
          fields.tool_config = null;
        } else {
          try {
            fields.tool_config = JSON.parse(cfgRaw);
          } catch {
            toast("Tool config is not valid JSON.", "err");
            return;
          }
        }
      }
      if (!Object.keys(fields).length) {
        toast("No changes.", "");
        return;
      }
      saveBtn.disabled = true;
      try {
        const res = await api.patch(`/nodes/${jobId}/${node.node_key}`, {
          ...fields,
          expected_version: node.edit_version,
        });
        if (disposed) return;
        const resetMsg = res.reset && res.reset.length ? ` — reset ${res.reset.length} node(s)` : "";
        toast(`Saved ${node.node_key}${resetMsg}.`, "ok");
        await load();
      } catch (e) {
        if (disposed) return;
        if (e.status === 409) {
          toast("Node was edited elsewhere — reloading fresh version.", "err");
          await reloadAndReopen(node.node_key);
        } else {
          toast(`Save failed: ${e.detail || e.message}`, "err");
          saveBtn.disabled = false;
        }
      }
    }
  }

  async function reloadAndReopen(key) {
    try {
      const res = await api.get(`/nodes/${jobId}`);
      if (disposed) return;
      nodes = res.nodes || [];
      byKey = Object.fromEntries(nodes.map((n) => [n.node_key, n]));
      graph.render(nodes);
      if (byKey[key]) openEditDrawer(byKey[key]);
      else closeDrawer();
    } catch (e) {
      if (!disposed) toast(`Reload failed: ${e.detail || e.message}`, "err");
    }
  }

  async function resetNode(key) {
    if (!confirm(`Reset ${key} to pending? This cascades to all downstream nodes.`)) return;
    try {
      const res = await api.post(`/nodes/${jobId}/${key}/reset`, {});
      if (disposed) return;
      toast(`Reset ${key}.`, "ok");
      await load();
    } catch (e) {
      if (!disposed) toast(`Reset failed: ${e.detail || e.message}`, "err");
    }
  }

  async function deleteNode(key) {
    if (!confirm(`Delete ${key}? Dependents are rewired and cascade-reset.`)) return;
    try {
      const res = await api.del(`/nodes/${jobId}/${key}`);
      if (disposed) return;
      const extra = res.rewired && res.rewired.length ? ` — rewired ${res.rewired.length}` : "";
      toast(`Deleted ${key}${extra}.`, "ok");
      closeDrawer();
      await load();
    } catch (e) {
      if (!disposed) toast(`Delete failed: ${e.detail || e.message}`, "err");
    }
  }

  // ── Insert drawer ────────────────────────────────────────────────
  function openInsertDrawer() {
    selectedKey = null;
    graph.clearSelected();
    drawer.classList.remove("hidden");

    const keyIn = el("input", { class: "input mono", placeholder: "e.g. T99" });
    const titleIn = el("input", { class: "input", placeholder: "Node title" });
    const descIn = el("textarea", { class: "input node-textarea", rows: 2 });
    const toolIn = el("input", { class: "input", value: "LLM" });
    const promptIn = el("textarea", { class: "input node-textarea", rows: 4 });
    const depsIn = depsSelect([], null);

    const addBtn = el("button", { class: "btn btn-sm btn-primary", text: "Insert", onClick: () => doInsert() });

    mount(
      drawer,
      el(
        "div",
        { class: "drawer-head" },
        el("div", { class: "row" }, el("span", { class: "tag", text: "new node" })),
        el("button", { class: "btn btn-sm btn-ghost drawer-close", text: "✕", onClick: () => closeDrawer() })
      ),
      el("h3", { class: "drawer-title", text: "Insert node" }),
      el(
        "div",
        { class: "node-form" },
        field("Node key", keyIn, "Unique within the job."),
        field("Title", titleIn),
        field("Description", descIn),
        field("Tool", toolIn),
        field("Prompt template", promptIn),
        field("Depends on", depsIn)
      ),
      el("div", { class: "drawer-actions" }, addBtn)
    );

    async function doInsert() {
      const node_key = keyIn.value.trim();
      const title = titleIn.value.trim();
      if (!node_key || !title) {
        toast("Node key and title are required.", "err");
        return;
      }
      addBtn.disabled = true;
      try {
        await api.post(`/nodes/${jobId}`, {
          node_key,
          title,
          description: descIn.value || null,
          tool: toolIn.value || "LLM",
          prompt_template: promptIn.value || null,
          depends_on: readDeps(depsIn),
        });
        if (disposed) return;
        toast(`Inserted ${node_key}.`, "ok");
        closeDrawer();
        await load();
      } catch (e) {
        if (!disposed) {
          toast(`Insert failed: ${e.detail || e.message}`, "err");
          addBtn.disabled = false;
        }
      }
    }
  }

  // ── Reorder panel ────────────────────────────────────────────────
  let reorderOpen = false;
  function toggleReorder() {
    reorderOpen = !reorderOpen;
    if (!reorderOpen) {
      reorderPanel.classList.add("hidden");
      return;
    }
    reorderPanel.classList.remove("hidden");
    let order = nodes.map((n) => n.node_key);

    function renderList() {
      mount(
        reorderPanel,
        el("div", { class: "reorder-head" }, el("strong", { text: "Execution order" }), el("button", { class: "btn btn-sm btn-primary", text: "Save order", onClick: () => saveOrder() }), el("button", { class: "btn btn-sm btn-ghost", text: "Close", onClick: () => toggleReorder() })),
        el(
          "ol",
          { class: "reorder-list" },
          ...order.map((k, i) =>
            el(
              "li",
              { class: "reorder-item" },
              el("span", { class: "mono", text: k }),
              el("span", { class: "faint", text: byKey[k] ? byKey[k].title || "" : "" }),
              el("span", { class: "spacer" }),
              el("button", { class: "btn btn-xs", text: "↑", disabled: i === 0, onClick: () => move(i, -1) }),
              el("button", { class: "btn btn-xs", text: "↓", disabled: i === order.length - 1, onClick: () => move(i, 1) })
            )
          )
        )
      );
    }
    function move(i, d) {
      if (moveItem(order, i, d)) renderList();
    }
    async function saveOrder() {
      try {
        await api.post(`/nodes/${jobId}/reorder`, { ordered_keys: order });
        if (disposed) return;
        toast("Order saved.", "ok");
        toggleReorder();
        await load();
      } catch (e) {
        if (!disposed) toast(`Reorder failed: ${e.detail || e.message}`, "err");
      }
    }
    renderList();
  }

  load();

  return () => {
    disposed = true;
    graph.destroy();
  };
}

export default function plan(container, params) {
  if (params && params.jobId) return renderPlan(container, params.jobId);
  return renderPlan(container, null);
}
