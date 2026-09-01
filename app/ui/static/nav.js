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
// §17.896 — condensed from 17 items / 5 peer groups to 11 everyday items in
// four open groups, with the six System entries behind one collapsed-by-default
// group. Two operator findings drove it:
//   - the DAG was unfindable. §17.859 collapsed the canvas into a job-hub TAB,
//     which is right, but nothing in the sidebar said "DAG" and the tab was
//     only reachable after picking a job from a list. The pinned Current-job
//     block in app.js (⬡ DAG · ▶ Run · ▤ Output) is the fix; this file's job
//     is to stop the sidebar competing with it for attention.
//   - Approvals was a peer destination for what is a STATUS, not a place. It
//     is now the "Awaiting approval" chip in Jobs (jobs.js), and #/approvals
//     still resolves for old links/bookmarks — it is just not sidebar chrome.
export const NAV_GROUPS = [
  {
    label: "Create",
    items: [
      { id: "new", path: "/new", label: "New idea", icon: "＋" },
      { id: "chat", path: "/chat", label: "Chat", icon: "💬", adminOnly: true },
    ],
  },
  {
    label: "Work",
    items: [
      { id: "dashboard", path: "/", label: "Dashboard", icon: "◈" },
      { id: "jobs", path: "/jobs", label: "Jobs", icon: "▣" },
      // §17.859 — DAG Canvas / Execution / Outputs collapsed into the job
      // hub's tabs (#/job/:id) — one job, one URL. Work = pick a job.
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
    // Config + observability: needed rarely, and never while driving a job.
    // `collapsed` is the DEFAULT only — app.js still honors an explicit
    // operator expand/collapse stored in scaffold_nav_closed.
    label: "System",
    collapsed: true,
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
