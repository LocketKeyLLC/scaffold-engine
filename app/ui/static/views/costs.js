// §17.816 (plan 5.4f) — Costs dashboard, promoted out of the compare view.
// System-wide rollup (GET /observability/llm, windowed, by provider+model with
// p50/p95/p99 latency) + recent per-job cost drill-down (GET /jobs/{id}/costs).
import * as api from "../api.js";
import { el, fmtNum, fmtUsd, mount, shortId, timeAgo } from "../util.js";
import { errorPanel, loading, statTile } from "../components.js";

const WINDOWS = [
  ["60", "last hour"],
  ["1440", "last 24 h"],
  ["10080", "last 7 d"],
];

export default function costs(container) {
  let disposed = false;

  const windowSel = el(
    "select",
    { class: "input input-sm" },
    ...WINDOWS.map(([v, label]) => el("option", { value: v, text: label, selected: v === "1440" }))
  );
  const rollupBox = el("div", {});
  const jobsBox = el("div", {});

  async function loadRollup() {
    mount(rollupBox, loading("Aggregating…"));
    try {
      const res = await api.get("/observability/llm", {
        query: { window_minutes: windowSel.value },
      });
      renderRollup(res);
    } catch (e) {
      mount(rollupBox, errorPanel(e, loadRollup));
    }
  }

  function renderRollup(res) {
    if (disposed) return;
    const rows = res.by_model || [];
    const totTokens = rows.reduce(
      (a, r) => a + (r.prompt_tokens || 0) + (r.completion_tokens || 0), 0);
    mount(
      rollupBox,
      el(
        "div",
        { class: "row costs-tiles" },
        statTile("Calls", fmtNum(res.total_calls)),
        statTile("Cost", fmtUsd(res.total_cost_usd)),
        statTile("Tokens", fmtNum(totTokens)),
        statTile("Models", fmtNum(rows.length))
      ),
      el(
        "table",
        { class: "table" },
        el("thead", {}, el("tr", {},
          el("th", { text: "Model" }), el("th", { text: "Calls" }),
          el("th", { text: "Fail" }), el("th", { text: "Cost" }),
          el("th", { text: "Tokens in/out" }), el("th", { text: "p50 / p95 (ms)" }))),
        el("tbody", {}, ...rows.map((r) =>
          el("tr", {},
            el("td", {}, el("code", { text: r.model }), el("span", { class: "faint", text: ` ${r.provider}` })),
            el("td", { text: fmtNum(r.calls) }),
            el("td", {}, r.failures
              ? el("span", { class: "badge err", text: String(r.failures) })
              : el("span", { class: "faint", text: "0" })),
            el("td", { text: fmtUsd(r.cost_usd) }),
            el("td", { text: `${fmtNum(r.prompt_tokens)} / ${fmtNum(r.completion_tokens)}` }),
            el("td", { text: `${fmtNum(r.latency_ms_p50)} / ${fmtNum(r.latency_ms_p95)}` }))
        ))
      )
    );
  }

  async function loadJobs() {
    mount(jobsBox, loading("Loading recent job costs…"));
    try {
      const res = await api.get("/jobs", { query: { limit: 10 } });
      const jobs = res.jobs || [];
      const withCosts = await Promise.all(
        jobs.map(async (j) => ({
          job: j,
          costs: await api.get(`/jobs/${j.id}/costs`).catch(() => null),
        }))
      );
      if (disposed) return;
      mount(
        jobsBox,
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {},
            el("th", { text: "Job" }), el("th", { text: "Status" }),
            el("th", { text: "Calls" }), el("th", { text: "Cost" }),
            el("th", { text: "Tokens in/out" }), el("th", { text: "Updated" }))),
          el("tbody", {}, ...withCosts.map(({ job, costs: c }) =>
            el("tr", {},
              el("td", {}, el("a", { href: `#/output/${job.id}`, text: job.title || shortId(job.id) })),
              el("td", {}, el("span", { class: "badge", text: job.status })),
              el("td", { text: fmtNum(c?.call_count) }),
              el("td", { text: fmtUsd(c?.total_cost_usd) }),
              el("td", {
                text: c ? `${fmtNum(c.total_prompt_tokens)} / ${fmtNum(c.total_completion_tokens)}` : "—",
              }),
              el("td", { class: "faint", text: timeAgo(job.updated_at || job.created_at) }))
          ))
        )
      );
    } catch (e) {
      mount(jobsBox, errorPanel(e, loadJobs));
    }
  }

  windowSel.addEventListener("change", loadRollup);

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Costs" }),
        el("div", {
          class: "sub",
          text: "System-wide LLM spend + latency by model, and recent per-job cost breakdowns.",
        })
      ),
      el("div", { class: "row" }, windowSel)
    ),
    rollupBox,
    el("h2", { class: "costs-h2", text: "Recent jobs" }),
    jobsBox
  );
  loadRollup();
  loadJobs();

  return () => {
    disposed = true;
  };
}
