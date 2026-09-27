// Plan editor — review + edit a generated DAG before execution. Reuses the
// shared graph controller (dag_render.js); the node drawer becomes an edit form
// wired to the node CRUD endpoints (PATCH/POST/DELETE/reorder/reset), with
// optimistic-lock (edit_version) 409 handling. "Execute plan" hands off to the
// execution theater. Reached via the approval gate's Approve chain.
import * as api from "../api.js";
import { jobStore } from "../store.js";
import { el, mount, moveItem, mdToHtml } from "../util.js";
import { statusBadge, loading, errorPanel, toast, openDialog, askConfirm } from "../components.js";
import { createGraphCanvas } from "./dag_render.js";
import { startControl } from "./job_hub.js";

// §17.815 — edit attribution is SERVER-derived from the API key (audit-trail
// integrity); the client no longer sends a spoofable label.

// Mirrors app/config.py VALID_TOOLS (tests/ui/plan_editor.test.mjs pins the set):
// what a step uses, in the operator's words.
export const TOOL_LABELS = {
  LLM: "The model (thinks, writes, plans)",
  Shell: "Your terminal (commands you run)",
  CodeGen: "Code generation (writes and checks code)",
  SearXNG: "Web search",
  Milvus: "The knowledge base",
  MCP: "An MCP tool",
};

// The next free step key: T<n> above every numeric T-key the plan has (engine
// insertions are ADD<n>; the two never collide). Pure, pinned by the node test.
export function nextNodeKey(keys) {
  let max = 0;
  const have = new Set(keys || []);
  for (const k of have) { const m = /^T(\d+)$/.exec(k); if (m) max = Math.max(max, Number(m[1])); }
  let n = max + 1;
  while (have.has(`T${n}`)) n += 1;
  return `T${n}`;
}

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
  graph.onNodeClick((key) => openEditDrawer(byKey[key]));   // opens the READ view; Edit is a verb inside

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

  // ── The step editor: read first, edit second ─────────────────────────
  // UX overhaul phase 5. The drawer opened straight onto raw fields ("Prompt
  // template", a free-text "Tool", a free-text "Assigned model", "Tool config
  // (JSON)") and inserting a step asked the operator to INVENT a node key. Now a
  // step opens as something to read — what it does, the instructions, what it
  // uses, what it comes after — with "Edit this step" and "Insert a step after
  // this" as the two verbs; the form names things in plain words, the tool is a
  // labelled choice, the model comes from the catalog, keys are generated.
  let catalog = null;   // model ids from /models/available (fail-soft: free text still works)
  async function modelCatalog() {
    if (catalog) return catalog;
    try {
      const res = await api.get("/models/available");
      catalog = res.reachable === false ? [] : (res.models || [...(res.local || []), ...(res.cloud || [])]);
    } catch (e) { console.debug("plan: model catalog unavailable — the model field stays free text", e); catalog = []; }
    return catalog;
  }
  const titleOf = (k) => (byKey[k] && byKey[k].title) || "";

  function depsPicker(current, excludeKey) {
    const chosen = new Set(current || []);
    const list = el("div", { class: "deps-picker" },
      ...nodes.filter((n) => n.node_key !== excludeKey).map((n) => {
        const cb = el("input", { type: "checkbox", value: n.node_key });
        if (chosen.has(n.node_key)) cb.checked = true;
        return el("label", { class: "deps-item" }, cb, el("span", { class: "mono faint deps-key", text: n.node_key }), el("span", { class: "deps-title", text: n.title || "" }));
      }));
    return { node: list, read: () => Array.from(list.querySelectorAll("input:checked")).map((i) => i.value) };
  }

  function toolSelect(current) {
    const sel = el("select", { class: "input" });
    for (const [k, label] of Object.entries(TOOL_LABELS)) sel.append(el("option", { value: k, text: label, selected: k === (current || "LLM") ? "" : null }));
    if (current && !TOOL_LABELS[current]) sel.append(el("option", { value: current, text: current, selected: "" }));
    return sel;
  }

  function modelField(current) {
    const listId = `plan-models-${jobId.slice(0, 8)}`;
    const datalist = el("datalist", { id: listId });
    modelCatalog().then((list) => mount(datalist, ...list.map((m) => el("option", { value: m }))));
    const input = el("input", { class: "input", value: current || "", placeholder: "the role's default model", list: listId });
    return { node: el("div", {}, input, datalist), read: () => input.value.trim() };
  }

  function readView(node) {
    const deps = node.depends_on || [];
    const tool = TOOL_LABELS[node.tool] || node.tool || "LLM";
    return el("div", { class: "step-read" },
      el("div", { class: "row row-wrap step-read-tags" },
        statusBadge(node.status),
        el("span", { class: "tag", text: tool }),
        node.is_deliverable ? el("span", { class: "tag deliverable", text: "★ part of the deliverable" }) : null,
        node.assigned_model ? el("span", { class: "tag mono", title: "Model for this step", text: String(node.assigned_model) }) : null),
      node.description ? el("div", { class: "md step-read-desc", html: mdToHtml(node.description) }) : el("p", { class: "dim", text: "No description." }),
      deps.length
        ? el("div", { class: "step-read-after" }, el("span", { class: "dim", text: "Comes after: " }),
            ...deps.map((k) => el("button", { class: "btn btn-sm btn-ghost step-dep", text: `${k} · ${titleOf(k).slice(0, 40)}`, title: titleOf(k), onClick: () => openEditDrawer(byKey[k]) })))
        : el("div", { class: "step-read-after dim", text: "A first step — nothing comes before it." }),
      node.prompt_template
        ? el("details", { class: "brief-details step-read-instructions" },
            el("summary", {}, "Instructions the engine follows for this step"),
            el("div", { class: "md", html: mdToHtml(node.prompt_template) }))
        : null,
      node.tool === "MCP" && node.tool_config
        ? el("details", { class: "brief-details" }, el("summary", {}, "Tool settings"), el("pre", { class: "md-pre json-pre", text: JSON.stringify(node.tool_config, null, 2) }))
        : null);
  }

  function openEditDrawer(node, { edit = false } = {}) {
    if (!node) return;
    selectedKey = node.node_key;
    graph.setSelected(node.node_key);
    drawer.classList.remove("hidden");
    queueMicrotask(armDrawer);   // §17.1118 — after this function mounts the fields

    const head = el("div", { class: "drawer-head" },
      el("div", { class: "row" }, el("span", { class: "mono faint", text: node.node_key }), el("span", { class: "tag", title: "Edit version", text: `v${node.edit_version}` })),
      el("button", { class: "btn btn-sm btn-ghost drawer-close", text: "✕", "aria-label": "Close editor", onClick: () => closeDrawer() }));
    const title = el("h3", { class: "drawer-title", text: node.title || "(untitled step)" });

    if (!edit) {
      const more = el("details", { class: "verbs-more step-more" },
        el("summary", { class: "btn btn-sm btn-ghost", text: "⋯", "aria-label": "More step actions", title: "Reset or delete this step" }),
        el("div", { class: "verbs-more-body" },
          el("button", { class: "btn btn-sm btn-ghost", text: "↺ Reset to not started", onClick: () => resetNode(node.node_key) }),
          el("button", { class: "btn btn-sm btn-ghost btn-quiet-danger", text: "🗑 Delete this step", onClick: () => deleteNode(node.node_key) })));
      mount(drawer, head, title, el("div", { class: "drawer-body" }, readView(node)),
        el("div", { class: "drawer-actions" },
          el("button", { class: "btn btn-sm btn-primary", text: "✎ Edit this step", onClick: () => openEditDrawer(node, { edit: true }) }),
          el("button", { class: "btn btn-sm", text: "＋ Insert a step after this", onClick: () => openInsertDrawer({ after: node.node_key }) }),
          el("span", { class: "spacer" }), more));
      return;
    }

    const titleIn = el("input", { class: "input", value: node.title || "" });
    const descIn = el("textarea", { class: "input node-textarea", rows: 3 }, node.description || "");
    const promptIn = el("textarea", { class: "input node-textarea", rows: 8 }, node.prompt_template || "");
    const toolIn = toolSelect(node.tool);
    const model = modelField(node.assigned_model);
    const delivIn = el("input", { type: "checkbox" });
    if (node.is_deliverable) delivIn.checked = true;
    const deps = depsPicker(node.depends_on, node.node_key);
    const cfgIn = el("textarea", { class: "input node-textarea mono", rows: 3 }, node.tool_config ? JSON.stringify(node.tool_config, null, 2) : "");
    const cfgField = field("Tool settings (JSON)", cfgIn, "Only for an MCP tool. Leave blank for none.");
    const syncCfg = () => { cfgField.hidden = toolIn.value !== "MCP"; };
    toolIn.addEventListener("change", syncCfg); syncCfg();

    const saveBtn = el("button", { class: "btn btn-sm btn-primary", text: "Save", onClick: () => save() });
    const cancelBtn = el("button", { class: "btn btn-sm btn-ghost", text: "Cancel", onClick: () => openEditDrawer(byKey[node.node_key] || node) });
    const ran = node.status !== "pending";

    mount(drawer, head, title,
      ran ? el("div", { class: "plan-warning drawer-warning" }, el("span", { class: "warn-icon", text: "⚠" }), el("span", { text: "This step has already run. Changing what it does or what it comes after resets it, and every step after it, to not started." })) : null,
      el("div", { class: "node-form" },
        field("Title", titleIn),
        field("What this step does", descIn, "One or two sentences, in plain words."),
        field("Instructions the engine follows", promptIn, "What the engine is told when it works this step, or what it walks you through."),
        field("It uses", toolIn),
        field("Model for this step", model.node, "Blank = the role's default. Pick from the catalog or type a model id."),
        field("Comes after", deps.node, "The steps that must finish before this one can start."),
        el("div", { class: "node-field row" }, delivIn, el("label", { class: "node-field-label inline", text: "Its result is part of the deliverable" })),
        cfgField),
      el("div", { class: "drawer-actions" }, saveBtn, cancelBtn));

    async function save() {
      const fields = {};
      if (titleIn.value !== (node.title || "")) fields.title = titleIn.value;
      if (descIn.value !== (node.description || "")) fields.description = descIn.value;
      if (promptIn.value !== (node.prompt_template || "")) fields.prompt_template = promptIn.value;
      if (toolIn.value !== (node.tool || "LLM")) fields.tool = toolIn.value;
      if (model.read() !== (node.assigned_model || "")) fields.assigned_model = model.read() || null;
      if (delivIn.checked !== !!node.is_deliverable) fields.is_deliverable = delivIn.checked;
      const newDeps = deps.read();
      if (JSON.stringify([...newDeps].sort()) !== JSON.stringify([...(node.depends_on || [])].sort())) fields.depends_on = newDeps;
      const cfgRaw = cfgIn.value.trim();
      const origCfg = node.tool_config ? JSON.stringify(node.tool_config, null, 2) : "";
      if (cfgIn.value !== origCfg) {
        if (!cfgRaw) fields.tool_config = null;
        else {
          try { fields.tool_config = JSON.parse(cfgRaw); }
          catch { toast("Tool settings are not valid JSON.", "err"); return; }
        }
      }
      if (!Object.keys(fields).length) { toast("No changes.", ""); openEditDrawer(node); return; }
      saveBtn.disabled = true;
      try {
        const res = await api.patch(`/nodes/${jobId}/${node.node_key}`, { ...fields, expected_version: node.edit_version });
        if (disposed) return;
        const n = res.reset && res.reset.length;
        toast(`Saved ${node.node_key}${n ? ` — ${n} step${n === 1 ? "" : "s"} reset to not started` : ""}.`, "ok");
        await load();
      } catch (e) {
        if (disposed) return;
        if (e.status === 409) {
          toast("This step was changed elsewhere — showing the fresh version.", "err");
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
    if (!await askConfirm("Every step after it is reset too, and what they produced is cleared.",
      { title: `Reset ${key} to not started?`, confirmText: "Reset it", danger: true })) return;
    try {
      await api.post(`/nodes/${jobId}/${key}/reset`, {});
      if (disposed) return;
      toast(`${key} is back to not started.`, "ok");
      await load();
    } catch (e) {
      if (!disposed) toast(`Reset failed: ${e.detail || e.message}`, "err");
    }
  }

  async function deleteNode(key) {
    if (!await askConfirm("The steps that came after it are re-wired around it and reset to not started.",
      { title: `Delete ${key}?`, confirmText: "Delete it", danger: true })) return;
    try {
      const res = await api.del(`/nodes/${jobId}/${key}`);
      if (disposed) return;
      const extra = res.rewired && res.rewired.length ? ` — re-wired ${res.rewired.length}` : "";
      toast(`Deleted ${key}${extra}.`, "ok");
      closeDrawer();
      await load();
    } catch (e) {
      if (!disposed) toast(`Delete failed: ${e.detail || e.message}`, "err");
    }
  }

  // ── Insert ─────────────────────────────────────────────────────────
  function openInsertDrawer({ after = null } = {}) {
    selectedKey = null;
    graph.clearSelected();
    drawer.classList.remove("hidden");
    queueMicrotask(armDrawer);   // §17.1118 — after this function mounts the fields

    const key = nextNodeKey(nodes.map((n) => n.node_key));
    const titleIn = el("input", { class: "input", placeholder: "What the step achieves, e.g. Install Jellyfin" });
    const descIn = el("textarea", { class: "input node-textarea", rows: 3, placeholder: "One or two sentences, in plain words." });
    const toolIn = toolSelect(after && byKey[after] ? byKey[after].tool : "LLM");
    const promptIn = el("textarea", { class: "input node-textarea", rows: 5, placeholder: "Optional — the engine writes a walkthrough from the title and description if you leave this blank." });
    const deps = depsPicker(after ? [after] : [], null);
    const addBtn = el("button", { class: "btn btn-sm btn-primary", text: "Add the step", onClick: () => doInsert() });

    mount(drawer,
      el("div", { class: "drawer-head" },
        el("div", { class: "row" }, el("span", { class: "tag", text: "new step" }), el("span", { class: "mono faint", text: key })),
        el("button", { class: "btn btn-sm btn-ghost drawer-close", text: "✕", "aria-label": "Close editor", onClick: () => closeDrawer() })),
      el("h3", { class: "drawer-title", text: after ? `Insert a step after ${after}` : "Insert a step" }),
      el("div", { class: "node-form" },
        field("Title", titleIn),
        field("What this step does", descIn),
        field("It uses", toolIn),
        field("Instructions the engine follows", promptIn),
        field("Comes after", deps.node, "The steps that must finish before this one can start.")),
      el("div", { class: "drawer-actions" }, addBtn, el("button", { class: "btn btn-sm btn-ghost", text: "Cancel", onClick: () => closeDrawer() })));
    titleIn.focus();

    async function doInsert() {
      const title = titleIn.value.trim();
      if (!title) { toast("Give the step a title.", "err"); titleIn.focus(); return; }
      addBtn.disabled = true;
      try {
        await api.post(`/nodes/${jobId}`, {
          node_key: key, title,
          description: descIn.value || null,
          tool: toolIn.value || "LLM",
          prompt_template: promptIn.value || null,
          depends_on: deps.read(),
        });
        if (disposed) return;
        toast(`Added ${key} — ${title}.`, "ok");
        closeDrawer();
        await load();
      } catch (e) {
        if (!disposed) {
          toast(`Could not add the step: ${e.detail || e.message}`, "err");
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
