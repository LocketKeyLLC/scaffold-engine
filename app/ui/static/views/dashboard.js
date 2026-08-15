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

/** Per-job navigation targets derived from status + node_count. */
function jobLinks(job) {
  const links = [];
  const sid = assistSessionFromActions(job.next_actions);
  if (sid) links.push({ label: "Assistant", href: `#/assist/${sid}` });
  if ((job.node_count || 0) > 0) {
    links.push({ label: "DAG", href: `#/dag/${job.id}` });
    links.push({ label: "Execution", href: `#/theater/${job.id}` });
  }
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
      el("span", { class: "spacer" }),
      ...links.map((l) => el("a", { class: "btn btn-sm btn-ghost", href: l.href, text: l.label }))
    )
  );
}

function recentRow(job) {
  const tr = el(
    "tr",
    { dataset: { href: (job.node_count || 0) > 0 ? `#/dag/${job.id}` : "" } },
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
      const [status, work] = await Promise.all([api.get("/status"), api.get("/work")]);
      if (disposed) return;
      render(status, work);
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, () => load()));
    }
  }

  function render(status, work) {
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
      el("div", { class: "section-head" }, el("h2", { text: "Recent jobs" })),
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

    mount(outlet, tiles, strip, workSection, recentSection);
  }

  load();
  timer = setInterval(load, 10000);

  return () => {
    disposed = true;
    if (timer) clearInterval(timer);
  };
}
