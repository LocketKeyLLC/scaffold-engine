// All-jobs browser — the destination every dashboard number links to.
// Route: #/jobs and #/jobs/:filter, where :filter is a semantic group
// (attention | running | terminal) or any raw job status. The dashboard's
// tiles and status-strip badges deep-link here; before this view existed
// they were display-only ("43 total jobs" had nowhere that showed 43 jobs).
import * as api from "../api.js";
import { el, mount, timeAgo } from "../util.js";
import { statusBadge, loading, errorPanel, makeClickable } from "../components.js";

const GROUPS = {
  attention: new Set(["awaiting_confirmation", "awaiting_assist", "assisted_paused", "blocked"]),
  running: new Set([
    "refining", "researching", "planning", "executing", "running",
    "assisted_executing", "assisted_running", "aggregating",
  ]),
  terminal: new Set(["completed", "cancelled", "failed"]),
};

const CHIPS = [
  ["all", "All"],
  ["attention", "Needs attention"],
  ["running", "Running"],
  ["completed", "Completed"],
  ["cancelled", "Cancelled"],
  ["failed", "Failed"],
];

function matches(job, filter) {
  if (!filter || filter === "all") return true;
  if (GROUPS[filter]) return GROUPS[filter].has(job.status);
  return job.status === filter;
}

/** Where a row should land — the job's most useful surface for its status.
 * (/jobs items carry no next_actions, so assist statuses route to the
 * assistant list rather than a specific session.) */
function jobHref(job) {
  // §17.859 — one job, one URL: the hub picks the right embedded surface
  // (Overview embeds the approval gate pre-DAG; Run embeds assist for
  // assisted_* statuses).
  if (["awaiting_assist", "assisted_paused", "assisted_executing", "assisted_running"].includes(job.status)) return `#/job/${job.id}/run`;
  if (job.status === "completed") return `#/job/${job.id}/output`;
  if ((job.node_count || 0) > 0) return `#/job/${job.id}/plan`;
  return `#/job/${job.id}`;
}

export default function jobs(container, params) {
  let filter = params?.filter || "all";
  let query = "";
  let all = [];
  let disposed = false;

  const outlet = el("div", {});
  const search = el("input", {
    class: "input input-sm jobs-search",
    placeholder: "Filter by title…",
    onInput: (e) => { query = e.target.value.trim().toLowerCase(); renderList(); },
  });

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el("div", {}, el("h1", { text: "Jobs" }), el("div", { class: "sub", text: "Every job, filterable — click a row to open it where it's actionable." })),
      el("div", { class: "header-actions" }, search)
    ),
    outlet
  );
  mount(outlet, loading("Loading jobs…"));

  function chipRow() {
    const count = (f) => all.filter((j) => matches(j, f)).length;
    return el(
      "div",
      { class: "row row-wrap jobs-chips" },
      ...CHIPS.map(([f, label]) =>
        el("button", {
          class: f === filter ? "btn btn-sm btn-primary" : "btn btn-sm",
          text: `${label} (${count(f)})`,
          onClick: () => {
            filter = f;
            // Keep the URL shareable/bookmarkable without re-running the router.
            history.replaceState(null, "", f === "all" ? "#/jobs" : `#/jobs/${f}`);
            renderList();
          },
        })
      )
    );
  }

  function row(job) {
    const tr = el(
      "tr",
      { class: "row-link" },
      el("td", {}, statusBadge(job.status)),
      el("td", { class: "recent-title" }, job.title || "(untitled)"),
      el("td", { class: "mono", text: String(job.node_count ?? "—") }),
      el("td", { class: "faint", text: timeAgo(job.updated_at || job.created_at) })
    );
    makeClickable(tr, () => { location.hash = jobHref(job); },  // §17.854 G6
      { role: "link", label: `Open ${job.title || "job"}` });
    return tr;
  }

  function renderList() {
    const visible = all.filter((j) => matches(j, filter) && (!query || (j.title || "").toLowerCase().includes(query)));
    mount(
      outlet,
      chipRow(),
      visible.length
        ? el(
            "div",
            { class: "card table-wrap" },
            el(
              "table",
              { class: "table" },
              el("thead", {}, el("tr", {}, el("th", { text: "Status" }), el("th", { text: "Title" }), el("th", { text: "Nodes" }), el("th", { text: "Updated" }))),
              el("tbody", {}, ...visible.map(row))
            )
          )
        : el("div", { class: "card empty-state small" }, el("p", { text: query ? "No jobs match that search." : "No jobs in this bucket." }))
    );
  }

  async function load() {
    try {
      // /jobs caps limit at 100 — page through, bounded at 500 rows.
      all = [];
      let offset = 0;
      for (;;) {
        const res = await api.get(`/jobs?limit=100&offset=${offset}`);
        if (disposed) return;
        const page = res.jobs || [];
        all.push(...page);
        offset += page.length;
        if (page.length < 100 || offset >= Math.min(res.total ?? offset, 500)) break;
      }
      renderList();
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, () => load()));
    }
  }

  load();
  return () => { disposed = true; };
}
