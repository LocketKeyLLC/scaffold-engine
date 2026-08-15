// Job comparison. Side-by-side of two jobs: status, node counts, cost/token/
// latency (GET /jobs/{id}/costs), and their compiled deliverables with an LCS
// line-diff. Picker (two searchable selectors) when both ids aren't in the route.
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount, shortId, debounce, fmtNum, fmtUsd } from "../util.js";
import { statusBadge, loading, errorPanel } from "../components.js";

const DIFF_LINE_CAP = 600; // above this, skip the O(n·m) LCS and show plain columns

// ── Picker ───────────────────────────────────────────────────────────
function renderPicker(container, prefillA) {
  let disposed = false;
  const sides = { a: prefillA ? { id: prefillA } : null, b: null };

  const compareBtn = el("button", { class: "btn btn-primary", text: "Compare", disabled: true, onClick: () => go() });

  function go() {
    if (sides.a && sides.b) router.navigate(`/compare/${sides.a.id}/${sides.b.id}`);
  }
  function refresh() {
    compareBtn.disabled = !(sides.a && sides.b);
  }

  function selector(which, label) {
    const input = el("input", { class: "input", placeholder: `Search jobs for side ${label}…` });
    const results = el("div", { class: "compare-results" });
    const chosen = el("div", { class: "compare-chosen" });

    function showChosen() {
      const s = sides[which];
      mount(chosen, s ? el("div", { class: "row" }, el("span", { class: "mono", text: shortId(s.id) }), el("span", { class: "faint", text: s.title || "" })) : el("span", { class: "dim", text: "none selected" }));
    }

    const search = debounce(async () => {
      const q = input.value.trim();
      if (!q) {
        mount(results);
        return;
      }
      try {
        const res = await api.get("/jobs", { query: { q, limit: 12 } });
        if (disposed) return;
        mount(
          results,
          ...(res.jobs || []).map((j) =>
            el(
              "button",
              {
                class: "compare-result",
                onClick: () => {
                  sides[which] = { id: j.id, title: j.title };
                  showChosen();
                  refresh();
                  mount(results);
                  input.value = "";
                },
              },
              statusBadge(j.status),
              el("span", { class: "compare-result-title", text: j.title || "(untitled)" }),
              el("span", { class: "faint mono", text: shortId(j.id) })
            )
          )
        );
      } catch {
        /* transient */
      }
    }, 250);
    input.addEventListener("input", search);

    showChosen();
    return el("div", { class: "compare-selector card card-pad" }, el("div", { class: "compare-selector-label", text: `Side ${label}` }), input, chosen, results);
  }

  mount(
    container,
    el("div", { class: "view-header" }, el("div", {}, el("h1", { text: "Compare Jobs" }), el("div", { class: "sub", text: "Pick two jobs to compare side by side" }))),
    el("div", { class: "compare-picker" }, selector("a", "A"), selector("b", "B")),
    el("div", { class: "compare-picker-actions" }, compareBtn)
  );
  refresh();

  return () => {
    disposed = true;
  };
}

// ── LCS line diff ────────────────────────────────────────────────────
function lcsDiff(aLines, bLines) {
  const n = aLines.length,
    m = bLines.length;
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = aLines[i] === bLines[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = [];
  let i = 0,
    j = 0;
  while (i < n && j < m) {
    if (aLines[i] === bLines[j]) {
      out.push({ t: "same", a: aLines[i], b: bLines[j] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ t: "del", a: aLines[i] });
      i++;
    } else {
      out.push({ t: "add", b: bLines[j] });
      j++;
    }
  }
  while (i < n) out.push({ t: "del", a: aLines[i++] });
  while (j < m) out.push({ t: "add", b: bLines[j++] });
  return out;
}

function diffTable(aText, bText) {
  const aLines = (aText || "").split("\n");
  const bLines = (bText || "").split("\n");
  const capped = aLines.length > DIFF_LINE_CAP || bLines.length > DIFF_LINE_CAP;
  const rows = capped
    ? // side-by-side without LCS alignment
      Array.from({ length: Math.max(aLines.length, bLines.length) }, (_, i) => ({ t: "same", a: aLines[i] ?? "", b: bLines[i] ?? "" }))
    : lcsDiff(aLines, bLines);

  const body = el(
    "div",
    { class: "diff-grid" },
    ...rows.flatMap((r) => [
      el("div", { class: "diff-cell diff-left" + (r.t === "del" ? " diff-del" : "") , text: r.a ?? "" }),
      el("div", { class: "diff-cell diff-right" + (r.t === "add" ? " diff-add" : ""), text: r.b ?? "" }),
    ])
  );
  return el(
    "div",
    { class: "card card-pad diff-wrap" },
    el("div", { class: "row" }, el("strong", { text: "Deliverable diff" }), capped ? el("span", { class: "faint", text: `— large output (${Math.max(aLines.length, bLines.length)} lines): showing aligned rows, no LCS` }) : null),
    el("div", { class: "diff-scroll" }, body)
  );
}

// ── Compare ──────────────────────────────────────────────────────────
async function fetchSide(id) {
  const [job, costs, logs] = await Promise.all([
    api.get(`/jobs/${id}`),
    api.get(`/jobs/${id}/costs`).catch(() => null),
    api.get(`/logs/${id}`, { query: { include_compiled: true } }).catch(() => ({})),
  ]);
  return { job, costs, compiled: (logs && logs.compiled_output) || "" };
}

function metrics(side) {
  const c = side.costs || {};
  const rows = [
    ["Status", null],
    ["Nodes", fmtNum(side.job.node_count)],
    ["Cost", fmtUsd(c.total_cost_usd)],
    ["Prompt tokens", fmtNum(c.total_prompt_tokens)],
    ["Completion tokens", fmtNum(c.total_completion_tokens)],
    ["Latency", c.total_latency_ms != null ? `${(c.total_latency_ms / 1000).toFixed(1)}s` : "—"],
    ["LLM calls", fmtNum(c.call_count)],
  ];
  return el(
    "div",
    { class: "compare-metrics" },
    ...rows.map(([k, v]) =>
      el(
        "div",
        { class: "compare-metric" },
        el("span", { class: "compare-metric-k", text: k }),
        k === "Status" ? statusBadge(side.job.status) : el("span", { class: "compare-metric-v mono", text: v })
      )
    )
  );
}

function renderCompare(container, aId, bId) {
  let disposed = false;
  const outlet = el("div", { class: "compare-view" }, loading("Loading both jobs…"));
  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el("div", {}, el("h1", { text: "Compare Jobs" }), el("div", { class: "sub mono", text: `${shortId(aId)} ⇄ ${shortId(bId)}` })),
      el("div", { class: "header-actions" }, el("a", { class: "btn btn-sm btn-ghost", href: "#/compare", text: "← Pick jobs" }))
    ),
    outlet
  );

  (async () => {
    try {
      const [A, B] = await Promise.all([fetchSide(aId), fetchSide(bId)]);
      if (disposed) return;
      const col = (side, otherId) =>
        el(
          "div",
          { class: "compare-col card card-pad" },
          el("div", { class: "row row-wrap" }, el("h2", { class: "compare-col-title", text: side.job.title || "(untitled)" })),
          el("div", { class: "faint mono", text: shortId(side.job.id) }),
          metrics(side),
          el("div", { class: "compare-links" }, el("a", { class: "btn btn-xs", href: `#/output/${side.job.id}`, text: "Output" }), el("a", { class: "btn btn-xs", href: `#/dag/${side.job.id}`, text: "DAG" }))
        );
      mount(
        outlet,
        el("div", { class: "compare-grid" }, col(A), col(B)),
        diffTable(A.compiled, B.compiled)
      );
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e, () => renderCompare(container, aId, bId)));
    }
  })();

  return () => {
    disposed = true;
  };
}

export default function compare(container, params) {
  if (params && params.jobA && params.jobB) return renderCompare(container, params.jobA, params.jobB);
  return renderPicker(container, params && params.jobA);
}
