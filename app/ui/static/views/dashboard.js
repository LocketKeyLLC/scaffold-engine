// Unified dashboard home — status overview, active work, recent jobs.
import * as api from "../api.js";
import { el, mount, timeAgo, fmtNum } from "../util.js";
import {
  statusBadge,
  statTile,
  loading,
  errorPanel,
  assistSessionFromActions,
} from "../components.js";

const ACTIVE = new Set([
  "refining",
  "researching",
  "planning",
  "executing",
  "running",
  "assisted_executing",
  "assisted_running",
  "aggregating",
]);
const ATTENTION = new Set([
  "awaiting_confirmation",
  "awaiting_assist",
  "assisted_paused",
  "blocked",
]);

// First-run onboarding: shown once when the install has zero jobs, then
// suppressed via this localStorage flag (survives navigation + reloads).
const ONBOARD_KEY = "scaffold_onboarded";

/** Per-job navigation targets derived from status + node_count. */
function jobLinks(job) {
  const links = [];
  const sid = assistSessionFromActions(job.next_actions);
  if (sid) links.push({ label: "Assistant", href: `#/assist/${sid}` });
  if (job.status === "awaiting_confirmation") links.push({ label: "Approve", href: `#/approvals/${job.id}` });
  if ((job.node_count || 0) > 0) {
    links.push({ label: "DAG", href: `#/dag/${job.id}` });
    links.push({ label: "Execution", href: `#/theater/${job.id}` });
  }
  if (job.status === "completed") links.push({ label: "Output", href: `#/output/${job.id}` });
  return links;
}

function workCard(job) {
  const links = jobLinks(job);
  return el(
    "div",
    { class: "card card-pad work-card" },
    el(
      "div",
      { class: "row row-wrap" },
      statusBadge(job.status),
      job.phase ? el("span", { class: "tag", text: job.phase }) : null,
      job.job_type && job.job_type !== "legacy" ? el("span", { class: "tag", text: job.job_type }) : null,
      el("span", { class: "spacer" }),
      el("span", { class: "faint", text: timeAgo(job.updated_at) })
    ),
    el("div", { class: "work-title", text: job.title || "(untitled)" }),
    el(
      "div",
      { class: "row row-wrap work-foot" },
      el("span", { class: "faint mono", text: `${job.node_count || 0} nodes` }),
      progressChip(job),
      el("span", { class: "spacer" }),
      ...links.map((l) => el("a", { class: "btn btn-sm btn-ghost", href: l.href, text: l.label }))
    )
  );
}

// §17.818 (plan 5.6) — live progress/ETA chip on in-flight cards, filled
// lazily from /exec/status (compute-on-read §17.811 snapshot). Only jobs
// actually executing get the extra request; everything else renders nothing.
function progressChip(job) {
  if (!["executing", "running", "assisted_executing", "assisted_running"].includes(job.status)) return null;
  const chip = el("span", { class: "tag prog-chip", text: "…" });
  api.get(`/exec/status/${job.id}`)
    .then((d) => {
      const pr = d.progress;
      if (!pr || pr.total == null) { chip.remove(); return; }
      chip.textContent = `${pr.pct ?? 0}%` + (pr.eta_human ? ` · ~${pr.eta_human}` : "");
      chip.title = pr.summary || "";
    })
    .catch(() => chip.remove());
  return chip;
}

function recentRow(job) {
  const recentHref = job.status === "completed" ? `#/output/${job.id}` : (job.node_count || 0) > 0 ? `#/dag/${job.id}` : "";
  const tr = el(
    "tr",
    { dataset: { href: recentHref } },
    el("td", {}, statusBadge(job.status)),
    el("td", { class: "recent-title" }, job.title || "(untitled)"),
    el("td", { class: "mono", text: String(job.node_count ?? "—") }),
    el("td", { class: "faint", text: timeAgo(job.updated_at || job.created_at) })
  );
  tr.addEventListener("click", () => {
    if (tr.dataset.href) location.hash = tr.dataset.href;
  });
  return tr;
}

export default function dashboard(container) {
  let timer = null;
  let disposed = false;

  const outlet = el("div", {});
  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el("div", {}, el("h1", { text: "Dashboard" }), el("div", { class: "sub", text: "System overview & active work" })),
      el(
        "div",
        { class: "header-actions" },
        el("span", { class: "auto-hint faint", text: "auto-refresh 10s" }),
        el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => load() })
      )
    ),
    outlet
  );
  mount(outlet, loading("Loading system status…"));

  async function load() {
    if (disposed) return;
    try {
      // Health + roles are enrichment — fail-soft to null so a degraded
      // orchestrator (or a non-admin key on /models/roles) still renders
      // the core dashboard.
      const [status, work, health, roles] = await Promise.all([
        api.get("/status"),
        api.get("/work"),
        api.health().catch(() => null),
        api.get("/models/roles").catch(() => null),
      ]);
      if (disposed) return;
      render(status, work, health, roles);
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, () => load()));
    }
  }

  // Quick actions — the "what do I do next" entry points.
  function quickActions(counts) {
    const awaiting = counts.awaiting_confirmation || 0;
    return el(
      "div",
      { class: "quick-actions" },
      el("a", { class: "btn btn-primary", href: "#/new", text: "＋ New idea" }),
      el(
        "a",
        { class: "btn", href: "#/approvals" },
        "⏻ Approvals",
        awaiting ? el("span", { class: "count-pill", text: String(awaiting) }) : null
      ),
      el("a", { class: "btn", href: "#/research", text: "◎ Research" }),
      el("a", { class: "btn", href: "#/assist", text: "✦ Assistant" })
    );
  }

  // Persistent setup checklist — rendered while /health carries advisory
  // warnings (unpulled role models, redis down, …); disappears when green.
  function setupChecklist(health) {
    const warns = health?.warnings || [];
    if (!warns.length) return null;
    return el(
      "div",
      { class: "card card-pad setup-checklist" },
      el(
        "div",
        { class: "setup-checklist-head" },
        el("span", { class: "setup-checklist-title", text: `Setup checklist — ${warns.length} item${warns.length === 1 ? "" : "s"} need attention` }),
        el("span", { class: "spacer" }),
        el("a", { class: "btn btn-sm", href: "#/setup", text: "Open Setup" })
      ),
      el("ul", { class: "setup-checklist-items" }, ...warns.map((w) => el("li", { text: w })))
    );
  }

  // System row — per-service health dots + current model-role bindings.
  function systemSection(health, roles) {
    const cards = [];
    const checks = Object.entries(health?.checks || {}).filter(
      ([, c]) => c && typeof c === "object" && typeof c.status === "string"
    );
    if (checks.length) {
      cards.push(
        el(
          "div",
          { class: "card card-pad" },
          el("div", { class: "section-head" }, el("h2", { text: "Services" })),
          el(
            "div",
            { class: "health-items" },
            ...checks.map(([name, c]) =>
              el(
                "span",
                { class: "health-item" },
                // Pass the real status through: "unknown" must render neutral
                // (CSS default gray), not a false-alarm red. Only a hard
                // "down"/"error" goes red.
                el("span", { class: "health-dot", dataset: { state: ["up", "degraded", "unknown"].includes(c.status) ? c.status : "down" } }),
                name,
                c.latency_ms != null ? el("span", { class: "faint", text: `${c.latency_ms} ms` }) : null
              )
            )
          )
        )
      );
    }
    const switchable = (roles?.roles || []).filter((r) => r.switchable);
    if (switchable.length) {
      cards.push(
        el(
          "div",
          { class: "card card-pad" },
          el(
            "div",
            { class: "section-head" },
            el("h2", { text: "Model roles" }),
            el("span", { class: "spacer" }),
            el("a", { class: "btn btn-ghost btn-sm", href: "#/models", text: "Manage" })
          ),
          el(
            "div",
            { class: "roles-list" },
            ...switchable.map((r) =>
              el("div", {}, el("span", { class: "role-k", text: r.role.replace("model_", "") }), r.model)
            )
          )
        )
      );
    }
    if (!cards.length) return null;
    return el("section", { class: "dash-section" }, el("div", { class: "grid grid-2" }, ...cards));
  }

  // First-run welcome: a 3-step orientation shown only on an empty install.
  function welcomeCard() {
    const step = (n, title, body) =>
      el(
        "div",
        { class: "welcome-step" },
        el("div", { class: "welcome-step-n", text: String(n) }),
        el(
          "div",
          {},
          el("div", { class: "welcome-step-t", text: title }),
          el("div", { class: "welcome-step-b dim", text: body })
        )
      );
    return el(
      "div",
      { class: "card card-pad welcome-card" },
      el("div", { class: "welcome-logo", text: "🧬" }),
      el("h2", { class: "welcome-title", text: "Welcome to Scaffold Engine" }),
      el("p", {
        class: "welcome-sub dim",
        text: "Turn an idea into a planned, executed multi-step workflow. Three steps to your first run:",
      }),
      el(
        "div",
        { class: "welcome-steps" },
        step(1, "Create an idea", "Describe what you want built — the engine triages it and refines it into a brief."),
        step(2, "Approve the plan", "Review the feasibility assessment and generated DAG, edit if needed, then approve."),
        step(3, "Watch it run", "Follow live execution node-by-node here, then collect the compiled output.")
      ),
      el(
        "div",
        { class: "welcome-actions row" },
        el("a", { class: "btn btn-primary", href: "#/new", text: "＋ Create your first idea" }),
        el("button", {
          class: "btn btn-ghost btn-sm",
          text: "Dismiss",
          onClick: () => {
            localStorage.setItem(ONBOARD_KEY, "1");
            load();
          },
        })
      )
    );
  }

  function render(status, work, health, roles) {
    // Empty install + not yet dismissed → orientation instead of zeroed tiles.
    if ((status.total_jobs || 0) === 0 && !localStorage.getItem(ONBOARD_KEY)) {
      mount(outlet, setupChecklist(health), welcomeCard());
      return;
    }
    const counts = status.status_counts || {};
    const sum = (set) => Object.entries(counts).reduce((a, [k, v]) => a + (set.has(k) ? v : 0), 0);
    const activeN = sum(ACTIVE);
    const attnN = sum(ATTENTION);

    const tiles = el(
      "div",
      { class: "grid grid-4" },
      statTile("Total jobs", fmtNum(status.total_jobs), { onClick: () => (location.hash = "#/") }),
      statTile("Active", fmtNum(activeN), { accent: "run" }),
      statTile("Needs attention", fmtNum(attnN), { accent: "warn" }),
      statTile("Completed", fmtNum(counts.completed || 0), { accent: "ok" })
    );

    // Status breakdown strip (only non-zero buckets)
    const nonzero = Object.entries(counts).filter(([, v]) => v > 0);
    const strip = el(
      "div",
      { class: "card card-pad status-strip" },
      ...(nonzero.length
        ? nonzero.map(([k, v]) =>
            el("span", { class: "strip-item" }, statusBadge(k), el("span", { class: "strip-n mono", text: String(v) }))
          )
        : [el("span", { class: "dim", text: "No jobs yet." })])
    );

    const workJobs = work.jobs || [];
    const workSection = el(
      "section",
      { class: "dash-section" },
      el(
        "div",
        { class: "section-head" },
        el("h2", { text: "Active work" }),
        el("span", { class: "count-pill", text: String(workJobs.length) })
      ),
      workJobs.length
        ? el("div", { class: "grid grid-3" }, ...workJobs.map(workCard))
        : el("div", { class: "card empty-state small" }, el("p", { text: "Nothing running right now." }))
    );

    const recent = status.recent_jobs || [];
    const recentSection = el(
      "section",
      { class: "dash-section" },
      el(
        "div",
        { class: "section-head" },
        el("h2", { text: "Recent jobs" }),
        el("span", { class: "spacer" }),
        el("a", { class: "btn btn-ghost btn-sm", href: "#/output", text: "All outputs →" })
      ),
      recent.length
        ? el(
            "div",
            { class: "card table-wrap" },
            el(
              "table",
              { class: "table" },
              el("thead", {}, el("tr", {}, el("th", { text: "Status" }), el("th", { text: "Title" }), el("th", { text: "Nodes" }), el("th", { text: "Updated" }))),
              el("tbody", {}, ...recent.map(recentRow))
            )
          )
        : el("div", { class: "card empty-state small" }, el("p", { text: "No recent jobs." }))
    );

    mount(
      outlet,
      quickActions(counts),
      setupChecklist(health),
      tiles,
      strip,
      workSection,
      systemSection(health, roles),
      recentSection
    );
  }

  load();
  // §17.818 — don't poll a hidden tab.
  timer = setInterval(() => { if (!document.hidden) load(); }, 10000);

  return () => {
    disposed = true;
    if (timer) clearInterval(timer);
  };
}
