// Plan editor — review + edit a generated DAG before execution. Reuses the
// shared graph controller (dag_render.js); the node drawer becomes an edit form
// wired to the node CRUD endpoints (PATCH/POST/DELETE/reorder/reset), with
// optimistic-lock (edit_version) 409 handling. "Execute plan" hands off to the
// execution theater. Reached via the approval gate's Approve chain.
import * as api from "../api.js";
import { jobStore } from "../store.js";
import { el, mount, moveItem } from "../util.js";
import { statusBadge, loading, errorPanel, toast, openDialog, askConfirm } from "../components.js";
import { createGraphCanvas } from "./dag_render.js";
import { startControl } from "./job_hub.js";

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

// §17.859 — embedded as the job hub's Plan tab (standalone route died with
// the hub). The read-only DAG canvas view folded in here too — this editor
// already renders the same graph via dag_render.js.
export function renderPlan(container, jobId, opts = {}) {
  let disposed = false;
  let nodes = [];
  let byKey = {};
  let jobStatus = null;
  let selectedKey = null;

  const warning = el("div", { class: "plan-warning hidden" });
  // UX overhaul — the toolbar is the plan's verbs only: how to start (the
  // split button carries the Auto/Assist choice, §17.853), fit, and the edit
  // verbs behind one menu. The stage strip above says where the job is.
  const editMenu = el("details", { class: "plan-edit-menu" },
    el("summary", { class: "btn btn-sm", text: "Edit steps ▾", "aria-label": "Edit steps", title: "Insert a step, or change the order" }),
    el("div", { class: "plan-edit-body" },
      el("button", { class: "btn btn-sm btn-ghost", text: "＋ Insert a step", onClick: () => { editMenu.open = false; openInsertDrawer(); } }),
      el("button", { class: "btn btn-sm btn-ghost", text: "↕ Reorder steps", onClick: () => { editMenu.open = false; toggleReorder(); } }),
      el("button", { class: "btn btn-sm btn-ghost", text: "↻ Reload", onClick: () => { editMenu.open = false; load(); } })));
  const onDocClick = (e) => { if (editMenu.open && !editMenu.contains(e.target)) editMenu.open = false; };
  document.addEventListener("click", onDocClick);
  const status = (opts.job && opts.job.status) || null;
  // a plan that is already being worked (or done) has no "start" — the Run stage owns it
  const startable = !status || ["executing", "planning", "awaiting_assist", "cancelled"].includes(status);
  const header = el(
    "div",
    { class: "row row-wrap plan-toolbar" },
    el("span", { class: "dim plan-lede", text: "Every box is a step, in the order the arrows give. Click one to read or edit what it will do." }),
    el("span", { class: "spacer" }),
    el("button", { class: "btn btn-sm", text: "Fit", onClick: () => graph.fit() }),
    editMenu,
    startable ? startControl(jobId) : null
  );

  const canvas = el("div", { class: "dag-canvas" });
  const drawer = el("div", { class: "dag-drawer hidden", role: "dialog", "aria-label": "Step editor" });
  let drawerDialog = null;   // §17.1118 — focus in on open, Escape closes, focus back on close
  const reorderPanel = el("div", { class: "reorder-panel hidden" });
  const stage = el("div", { class: "dag-stage" }, canvas, drawer);
  // Shown only at ≤820px (CSS): plan editing is a desktop-first surface.
  const mobileNote = el(
    "div",
    { class: "mobile-note" },
    "✎ Editing the plan is easiest on a wider screen. You can still tap a node to review it."
  );
  // The explainer card, the flow guide and the brief editor that used to sit
  // above the canvas are gone from here: the stage strip says where the job
  // is, the one-line lede says what the boxes are, and the brief lives under
  // ⋯ › Details. The approval answers stay reachable there too.
  // §17.1047 — the plan change ledger: what the reconciliation triggers
  // (a confirmed fix, a decision, a note, a re-pinned value) changed in the
  // steps ahead, per step before → after, with revert where still possible.
  const changesPanel = el("div", { class: "plan-changes" });
  async function loadChanges() {
    try {
      const res = await api.get(`/jobs/${jobId}/reconciliation`);
      const entries = (res && res.entries) || [];
      if (!entries.length) { mount(changesPanel); return; }
      const label = (e) => ({ fix_confirmed: "Fix confirmed", decision: "Decision", note: "Your note", substitution: "Environment pin", model_proposed: "Proposed after a fix (you approved)" })[e.trigger] || e.trigger;
      const rows = entries.slice().reverse().map((e) => {
        const head = el("div", { class: "plan-change-head" },
          el("span", { class: "tag", text: label(e) }),
          el("span", { class: "mono", text: e.source_node_key ? ` ${e.source_node_key}` : "" }),
          el("span", { class: "faint", text: ` · ${e.at ? new Date(e.at).toLocaleString() : ""}` }),
          e.reverted_at ? el("span", { class: "tag", text: "reverted" }) : null);
        const what = e.trigger === "decision"
          ? el("div", { class: "plan-change-what", text: `chosen (${e.chosen?.n}) ${e.chosen?.label}; not ${(e.rejected || []).map((o) => `(${o.n}) ${o.label}`).join("; ") || "none"}` })
          : el("div", { class: "plan-change-what" }, ...(e.corrections || []).map((c) => el("div", {}, el("code", { text: c.old }), " → ", el("code", { text: c.new }), el("span", { class: "faint", text: ` (${c.kind})` }))));
        const diffs = (e.changes || []).map((ch) => el("details", { class: "plan-change-node" },
          el("summary", {}, el("span", { class: "mono", text: ch.node_key }), el("span", { class: "faint", text: (e.revertable || []).includes(ch.node_key) ? " · revertable" : "" })),
          el("div", { class: "plan-change-diff" },
            el("pre", { class: "md-pre plan-change-before", text: ch.before || "" }),
            el("pre", { class: "md-pre plan-change-after", text: ch.after || "" }))));
        const resets = (e.guidance_resets || []).length
          ? el("div", { class: "faint", text: `walkthroughs rewritten: ${e.guidance_resets.join(", ")}` }) : null;
        const revertBtn = (!e.reverted_at && (e.revertable || []).length)
          ? el("button", { class: "btn btn-sm", text: `↩ Revert in ${e.revertable.join(", ")}`, onclick: async () => {
              try {
                const r = await api.post(`/jobs/${jobId}/reconciliation/${e.index}/revert`, {});
                toast(r.reverted?.length ? `Reverted ${r.reverted.join(", ")}.` : "Nothing left to revert.", "ok");
                await loadChanges(); load();
              } catch (err) { toast(`Could not revert: ${err.detail || err.message}`, "err"); }
            } })
          : null;
        return el("div", { class: "plan-change" }, head, what, ...diffs, resets, revertBtn);
      });
      mount(changesPanel, el("details", { class: "brief-details plan-changes-details" },
        el("summary", {}, `Plan changes (${entries.length}) — what the confirmed fixes, decisions, notes and pins changed in the steps ahead`),
        ...rows));
    } catch (e) {
      // §17.1117 (ledger U-9) — an unreadable ledger used to look like "no
      // plan changes"; say so, and offer the retry.
      mount(changesPanel, el("div", { class: "card warn-inline" },
        el("span", { text: `⚠ Could not load the plan change ledger: ${e.detail || e.message}. ` }),
        el("button", { class: "btn btn-sm", text: "Retry", onClick: () => loadChanges() })));
    }
  }
  loadChanges();
  mount(container, header, mobileNote, reorderPanel, stage, warning, changesPanel);
  mount(canvas, loading("Loading plan…"));

  const graph = createGraphCanvas(canvas);
  graph.onNodeClick((key) => openEditDrawer(byKey[key]));

  function closeDrawer() {
    drawer.classList.add("hidden");
    selectedKey = null;
    graph.clearSelected();
    if (drawerDialog) { drawerDialog.close(); drawerDialog = null; }
  }
  function armDrawer() {
    if (drawerDialog) drawerDialog.close();
    drawerDialog = openDialog(drawer, { label: "Step editor", onClose: closeDrawer, trap: false });
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
    queueMicrotask(armDrawer);   // §17.1118 — after this function mounts the fields

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
        el("button", { class: "btn btn-sm btn-ghost drawer-close", text: "✕", "aria-label": "Close editor", onClick: () => closeDrawer() })
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
    if (!await askConfirm("Every downstream node is reset too, and their outputs are cleared.",
      { title: `Reset ${key} to pending?`, confirmText: "Reset it", danger: true })) return;
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
    if (!await askConfirm("Dependents are rewired around it and cascade-reset.",
      { title: `Delete ${key}?`, confirmText: "Delete it", danger: true })) return;
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
    queueMicrotask(armDrawer);   // §17.1118 — after this function mounts the fields

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
        el("button", { class: "btn btn-sm btn-ghost drawer-close", text: "✕", "aria-label": "Close editor", onClick: () => closeDrawer() })
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
    document.removeEventListener("click", onDocClick);
    graph.destroy();
  };
}
