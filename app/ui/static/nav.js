// Grouped navigation — the single source of truth for the sidebar AND the
// command palette (which previously kept its own drifting copy). Groups are
// the operator-picked 5-way activity split (UI design phase).
//
// adminOnly notes:
// - chat (§17.815): native chat rides /v1, which is admin-only BY DESIGN
//   (§17.810 — the loopback re-auths as master; scoped keys without
//   identity-forwarding would be an escalation).
// - models/settings (§17.816): global engine config; writes are
//   require_admin server-side.
// - setup (§17.817): the wizard writes model roles via the models API.
export const NAV_GROUPS = [
  {
    label: "Create",
    items: [
      { id: "new", path: "/new", label: "New idea", icon: "＋" },
      { id: "chat", path: "/chat", label: "Chat", icon: "💬", adminOnly: true },
    ],
  },
  {
    label: "Operate",
    items: [
      { id: "dashboard", path: "/", label: "Dashboard", icon: "◈" },
      { id: "approvals", path: "/approvals", label: "Approvals", icon: "⏻" },
      { id: "dag", path: "/dag", label: "DAG Canvas", icon: "⬡" },
      { id: "theater", path: "/theater", label: "Execution", icon: "▶" },
      { id: "output", path: "/output", label: "Outputs", icon: "▤" },
      { id: "compare", path: "/compare", label: "Compare", icon: "⇄" },
    ],
  },
  {
    label: "Knowledge",
    items: [
      { id: "research", path: "/research", label: "Research", icon: "◎" },
      { id: "rag", path: "/rag", label: "Knowledge", icon: "◉" },
      { id: "library", path: "/library", label: "Library", icon: "❒" },
    ],
  },
  {
    label: "Automation",
    items: [
      { id: "assist", path: "/assist", label: "Assistant", icon: "✦" },
      { id: "schedules", path: "/schedules", label: "Schedules", icon: "◷" },
    ],
  },
  {
    label: "System",
    items: [
      { id: "models", path: "/models", label: "Models", icon: "⚙", adminOnly: true },
      { id: "costs", path: "/costs", label: "Costs", icon: "◍" },
      { id: "traces", path: "/traces", label: "Traces", icon: "≣", adminOnly: true },
      { id: "alerts", path: "/alerts", label: "Alerts", icon: "⚑", adminOnly: true },
      { id: "settings", path: "/settings", label: "Settings", icon: "☰", adminOnly: true },
      { id: "setup", path: "/setup", label: "Setup", icon: "✓", adminOnly: true },
    ],
  },
];

/** Flat item list in display order (route highlighting, palette commands). */
export const NAV = NAV_GROUPS.flatMap((g) => g.items);
