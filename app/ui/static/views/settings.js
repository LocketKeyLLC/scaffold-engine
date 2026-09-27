// Settings — everything that is not a job, behind one gear. Models,
// Capabilities, Status (services + model roles, once the Dashboard's bottom
// half), Costs, Traces, Alerts, Config (the read-only effective config) and
// Preferences (theme, density, alerts, sign-out — once the sidebar footer).
// Each tab is the existing view rendered in embedded mode.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { loading, errorPanel } from "../components.js";
import { hubTabs } from "./knowledge.js";

// [key, label, adminOnly]
export const TABS = [
  ["models", "Models", true],
  ["capabilities", "Capabilities", true],
  ["status", "Status", false],
  ["costs", "Costs", false],
  ["traces", "Traces", true],
  ["alerts", "Alerts", true],
  ["config", "Config", true],
  ["preferences", "Preferences", false],
];
export const TAB_ALIASES = { model: "models", roles: "models", connections: "models", health: "status", services: "status", cost: "costs", trace: "traces", alert: "alerts", settings: "config", prefs: "preferences", theme: "preferences" };

export function visibleTabs(isAdmin) {
  return TABS.filter(([, , admin]) => !admin || isAdmin).map(([k, l]) => [k, l]);
}
export function resolveTab(raw, isAdmin = true) {
  const t = TAB_ALIASES[raw] || raw;
  const vis = visibleTabs(isAdmin);
  if (vis.some(([k]) => k === t)) return t;
  return vis[0][0];
}

const LOADERS = {
  models: () => import("./models.js"),
  capabilities: () => import("./capabilities.js"),
  status: () => import("./status.js"),
  costs: () => import("./costs.js"),
  traces: () => import("./traces.js"),
  alerts: () => import("./alerts.js"),
  config: () => import("./config.js"),
  preferences: () => import("./preferences.js"),
};

export default function settings(container, params) {
  // §17.815 — unknown principal (pre-§17.815 server) fails open to admin; the
  // server still enforces authz on every write.
  const isAdmin = api.principal()?.is_admin !== false;
  const tab = resolveTab(params && params.tab, isAdmin);
  let disposed = false;
  let childDispose = null;
  const outlet = el("div", { class: "hub-outlet" }, loading("Loading…"));
  mount(
    container,
    el("div", { class: "view-header" },
      el("div", {}, el("h1", { text: "Settings" }), el("div", { class: "sub", text: "Models, optional capabilities, the engine's health, spend, and this browser's preferences." }))),
    hubTabs(visibleTabs(isAdmin), tab, (k) => `#/settings/${k}`),
    outlet
  );
  LOADERS[tab]().then((mod) => {
    if (disposed) return;
    childDispose = mod.default(outlet, params || {}, { embedded: true });
  }).catch((e) => {
    console.error(`[ui] failed to load settings tab "${tab}":`, e);
    if (!disposed) mount(outlet, errorPanel(e));
  });
  return () => { disposed = true; if (typeof childDispose === "function") childDispose(); };
}
