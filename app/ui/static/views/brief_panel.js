// §17.845 — the living PROJECT BRIEF panel.
//
// The brief is the operator-established truth every generation reads (assist
// funnel §17.844, DAG generation, execution compile). Circumstances change
// mid-project — hardware appears, constraints tighten — so the operator can
// add/remove items per section and reword the description here. Saves via
// PATCH /jobs/{id}/brief (sections replace; approval-gate answers stay an
// immutable receipt). Reused on the assist page and the plan editor.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { toast } from "../components.js";

const SECTIONS = [
  ["constraints", "Constraints"],
  ["inputs_available", "Available hardware / inputs"],
  ["goals", "Goals"],
  ["outputs_expected", "Expected outputs"],
];

export function briefPanel(jobId) {
  const root = el("div", { class: "card card-pad brief-panel" });
  const body = el("div", { class: "brief-panel-body" }, el("span", { class: "dim", text: "Loading brief…" }));
  const saveBtn = el("button", { class: "btn btn-sm btn-primary", text: "Save brief", disabled: "" });
  const status = el("span", { class: "faint brief-panel-status" });
  let brief = {};
  const lists = {};   // section key -> working array
  let descBox = null;
  let dirty = false;

  function markDirty() {
    dirty = true;
    saveBtn.disabled = false;
    status.textContent = "unsaved changes";
  }

  function sectionBlock(key, label) {
    const items = lists[key];
    const listEl = el("div", { class: "bp-items" });
    const renderItems = () => {
      mount(
        listEl,
        ...items.map((text, i) =>
          el(
            "div",
            { class: "bp-item" },
            el("span", { class: "bp-item-text", text }),
            el("button", {
              class: "btn btn-ghost btn-sm bp-remove",
              text: "✕",
              title: "Remove",
              onClick: () => { items.splice(i, 1); markDirty(); renderItems(); },
            })
          )
        )
      );
    };
    renderItems();
    const addInput = el("input", { class: "input input-sm bp-add", placeholder: `Add to ${label.toLowerCase()}…` });
    const add = () => {
      const v = addInput.value.trim();
      if (!v) return;
      items.push(v);
      addInput.value = "";
      markDirty();
      renderItems();
    };
    addInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
    return el(
      "details",
      { class: "brief-details bp-section", open: items.length && key === "constraints" ? "" : undefined },
      el("summary", {}, `${label} (${items.length})`),
      listEl,
      el("div", { class: "row bp-add-row" }, addInput, el("button", { class: "btn btn-sm", text: "＋ Add", onClick: add }))
    );
  }

  function render() {
    SECTIONS.forEach(([k]) => { lists[k] = [...(brief[k] || [])]; });
    descBox = el("textarea", {
      class: "input bp-desc",
      rows: "3",
      onInput: markDirty,
    });
    descBox.value = brief.description || "";
    mount(
      body,
      el("label", { class: "compose-label", text: "Description" }),
      descBox,
      ...SECTIONS.map(([k, label]) => sectionBlock(k, label)),
      brief.user_feedback
        ? el(
            "details",
            { class: "brief-details" },
            el("summary", {}, "Approval-gate answers (read-only receipt)"),
            el("pre", { class: "md-pre feedback-receipt", text: brief.user_feedback })
          )
        : null
    );
  }

  saveBtn.addEventListener("click", async () => {
    if (!dirty) return;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      const payload = { description: descBox.value.trim() };
      SECTIONS.forEach(([k]) => { payload[k] = lists[k]; });
      const res = await api.req(`/jobs/${jobId}/brief`, { method: "PATCH", body: payload });
      brief = res.refined_brief || brief;
      dirty = false;
      status.textContent = "saved — future guidance uses the updated brief";
      toast("Project brief saved.", "ok");
      render();
    } catch (e) {
      toast(`Save failed: ${e.detail || e.message}`, "err");
      saveBtn.disabled = false;
    } finally {
      saveBtn.textContent = "Save brief";
    }
  });

  (async () => {
    try {
      const job = await api.get(`/jobs/${jobId}`);
      brief = job.refined_brief || {};
      render();
    } catch (e) {
      mount(body, el("span", { class: "dim", text: `Could not load brief: ${e.detail || e.message}` }));
    }
  })();

  mount(
    root,
    el(
      "div",
      { class: "row" },
      el("div", { class: "side-title", text: "Project brief" }),
      el("span", { class: "spacer" }),
      status,
      saveBtn
    ),
    el("p", { class: "dim bp-hint", text: "The facts every step of guidance and planning reads. Edit as circumstances change — additions apply from the next generated step onward." }),
    body
  );
  return root;
}
