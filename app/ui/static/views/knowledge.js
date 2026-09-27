// Knowledge — the corpus, on one page. Search (RAG), Research runs, the
// Library (ground truths + artifacts) and Schedules were four sidebar
// destinations for one body of knowledge; they are tabs here. Each tab is the
// existing view rendered in embedded mode (no page header of its own).
import { el, mount } from "../util.js";
import { loading, errorPanel } from "../components.js";

export const TABS = [
  ["search", "Search"],
  ["research", "Research"],
  ["library", "Library"],
  ["schedules", "Schedules"],
];
// Old routes and their synonyms land on the right tab.
export const TAB_ALIASES = { rag: "search", runs: "research", gt: "library", artifacts: "library", schedule: "schedules" };
export function resolveTab(raw) {
  const t = TAB_ALIASES[raw] || raw || "search";
  return TABS.some(([k]) => k === t) ? t : "search";
}

const LOADERS = {
  search: () => import("./rag.js"),
  research: () => import("./research.js"),
  library: () => import("./library.js"),
  schedules: () => import("./schedules.js"),
};

export function hubTabs(tabs, active, hrefFor) {
  return el("div", { class: "job-tabs hub-tabs", role: "tablist", onKeydown: (e) => {   // §17.1118 — arrow keys move between tabs
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const items = Array.from(e.currentTarget.querySelectorAll('[role="tab"]'));
    const i = items.indexOf(document.activeElement);
    if (i === -1) return;
    e.preventDefault();
    const next = items[(i + (e.key === "ArrowRight" ? 1 : items.length - 1)) % items.length];
    next.focus(); next.click();
  } }, ...tabs.map(([key, label]) => el("a", {
    class: "job-tab" + (key === active ? " active" : ""),
    href: hrefFor(key), text: label, role: "tab", "aria-selected": key === active ? "true" : "false",
  })));
}

export default function knowledge(container, params) {
  const tab = resolveTab(params && params.tab);
  let disposed = false;
  let childDispose = null;
  const outlet = el("div", { class: "hub-outlet" }, loading("Loading…"));
  mount(
    container,
    el("div", { class: "view-header" },
      el("div", {}, el("h1", { text: "Knowledge" }), el("div", { class: "sub", text: "What the engine has read and learned — search it, grow it, keep it current." }))),
    hubTabs(TABS, tab, (k) => `#/knowledge/${k}`),
    outlet
  );
  LOADERS[tab]().then((mod) => {
    if (disposed) return;
    childDispose = mod.default(outlet, params || {}, { embedded: true });
  }).catch((e) => {
    console.error(`[ui] failed to load knowledge tab "${tab}":`, e);
    if (!disposed) mount(outlet, errorPanel(e));
  });
  return () => { disposed = true; if (typeof childDispose === "function") childDispose(); };
}
