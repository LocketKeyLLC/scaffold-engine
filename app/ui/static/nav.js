// The navigation model — the single source for the rail AND the command
// palette (which once kept its own drifting copy).
//
// UX overhaul (2026-09-27): three PLACES instead of nineteen destinations.
//   ◈ Home       — your jobs as next actions (was Dashboard + Jobs + Assistant
//                  + Approvals + Compare picker)
//   ◉ Knowledge  — the corpus: search, research runs, library, schedules
//   ⚙ Settings   — models, capabilities, status, costs, traces, alerts, config
// Everything else is a STAGE of a job (#/job/:id/<stage>) or a tab inside a
// place. `PAGES` lists every jumpable page for the palette, tabs included.
//
// adminOnly notes (unchanged):
// - the native chat (§17.815) rides /v1, admin-only BY DESIGN; it is the
//   "Ask the engine" verb on New idea now, offered only to an admin key.
// - models/config (§17.816): global engine config; writes are require_admin.
// - setup (§17.817): the wizard writes model roles via the models API.
export const NAV_GROUPS = [
  {
    label: "Places",
    items: [
      { id: "home", path: "/", label: "Home", icon: "◈" },
      { id: "knowledge", path: "/knowledge", label: "Knowledge", icon: "◉" },
      { id: "settings", path: "/settings", label: "Settings", icon: "⚙" },
    ],
  },
];

/** Flat item list in display order (route highlighting, the rail). */
export const NAV = NAV_GROUPS.flatMap((g) => g.items);

/** Every page the palette can jump to: the places plus their tabs. */
export const PAGES = [
  { id: "new", path: "/new", label: "New idea", icon: "＋" },
  ...NAV,
  { id: "knowledge-search", path: "/knowledge/search", label: "Knowledge · Search", icon: "◉" },
  { id: "knowledge-research", path: "/knowledge/research", label: "Knowledge · Research", icon: "◎" },
  { id: "knowledge-library", path: "/knowledge/library", label: "Knowledge · Library", icon: "❒" },
  { id: "knowledge-schedules", path: "/knowledge/schedules", label: "Knowledge · Schedules", icon: "◷" },
  { id: "settings-models", path: "/settings/models", label: "Settings · Models", icon: "⚙", adminOnly: true },
  { id: "settings-machines", path: "/settings/machines", label: "Settings · Machines", icon: "▤", adminOnly: true },
  { id: "settings-capabilities", path: "/settings/capabilities", label: "Settings · Capabilities", icon: "⚡", adminOnly: true },
  { id: "settings-status", path: "/settings/status", label: "Settings · Status", icon: "●" },
  { id: "settings-costs", path: "/settings/costs", label: "Settings · Costs", icon: "◍" },
  { id: "settings-traces", path: "/settings/traces", label: "Settings · Traces", icon: "≣", adminOnly: true },
  { id: "settings-alerts", path: "/settings/alerts", label: "Settings · Alerts", icon: "⚑", adminOnly: true },
  { id: "settings-config", path: "/settings/config", label: "Settings · Config", icon: "☰", adminOnly: true },
  { id: "settings-preferences", path: "/settings/preferences", label: "Settings · Preferences", icon: "◐" },
  { id: "setup", path: "/setup", label: "Setup wizard", icon: "✓", adminOnly: true },
];

// Which place a route belongs to (rail highlight). Job pages and the composer
// are Home's; the old per-page routes map onto the place that now holds them.
export const PLACE_OF = {
  "": "home", home: "home", new: "home", chat: "home", job: "home", jobs: "home",
  approvals: "home", assist: "home", compare: "home",
  knowledge: "knowledge", research: "knowledge", rag: "knowledge", library: "knowledge", schedules: "knowledge",
  settings: "settings", models: "settings", costs: "settings", traces: "settings", alerts: "settings",
  capabilities: "settings", setup: "settings", config: "settings",
};

export function placeOf(path) {
  const seg = String(path || "/").split("/").filter(Boolean)[0] || "";
  return PLACE_OF[seg] || "home";
}
