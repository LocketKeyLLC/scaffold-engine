// §17.816 (plan 5.4c) — Settings view: READ-ONLY effective config inventory
// over GET /config (name, live value w/ server-side secret redaction, type,
// default, overridden-vs-default, description). Editing stays with env/compose
// per the plan ("read-only effective config first, editable knobs later").
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { errorPanel, loading } from "../components.js";

export default function settings(container) {
  let disposed = false;
  let all = [];

  const filter = el("input", {
    class: "input settings-filter",
    placeholder: "Filter by name / description… (e.g. assist_, research_, model_)",
  });
  const onlyOverridden = el("input", { type: "checkbox" });
  const countEl = el("span", { class: "faint" });
  const tableBox = el("div", {});

  function render() {
    if (disposed) return;
    const q = filter.value.trim().toLowerCase();
    const rows = all.filter((f) => {
      if (onlyOverridden.checked && f.is_default) return false;
      if (!q) return true;
      return f.name.toLowerCase().includes(q) || (f.description || "").toLowerCase().includes(q);
    });
    countEl.textContent = ` ${rows.length} / ${all.length} settings`;
    mount(
      tableBox,
      el(
        "table",
        { class: "table settings-table" },
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            el("th", { text: "Setting" }),
            el("th", { text: "Value" }),
            el("th", { text: "Default" }),
            el("th", { text: "" })
          )
        ),
        el(
          "tbody",
          {},
          ...rows.slice(0, 400).map((f) =>
            el(
              "tr",
              { title: f.description || "" },
              el("td", {}, el("code", { text: f.name })),
              el("td", { class: "settings-val" }, el("code", { text: String(f.value) })),
              el("td", { class: "settings-val faint" }, el("code", { text: String(f.default) })),
              el("td", {}, f.is_default ? null : el("span", { class: "badge warn", text: "overridden" }))
            )
          )
        )
      )
    );
  }

  async function load() {
    mount(tableBox, loading("Loading effective config…"));
    try {
      const res = await api.get("/config");
      all = res.fields || res.settings || [];
      render();
    } catch (e) {
      mount(tableBox, errorPanel(e, load));
    }
  }

  filter.addEventListener("input", render);
  onlyOverridden.addEventListener("change", render);

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Settings" }),
        el("div", {
          class: "sub",
          text: "Read-only effective configuration (secrets redacted server-side). Hover a row for its description; change values via .env / compose and restart.",
        })
      )
    ),
    el(
      "div",
      { class: "card card-pad" },
      el(
        "div",
        { class: "row settings-controls" },
        filter,
        el("label", { class: "row faint settings-check" }, onlyOverridden, " only overridden"),
        countEl
      )
    ),
    tableBox
  );
  load();
  filter.focus();

  return () => {
    disposed = true;
  };
}
