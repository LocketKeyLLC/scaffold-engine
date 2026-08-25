// §17.816 (plan 5.4g) — Library view: the GT (ground-truth) corpus browser +
// per-job artifacts, previously OWUI/CLI-only. Two tabs: GT (paginated
// /gt/list w/ domain filter + /gt/stats tiles + expandable /gt/detail) and
// Artifacts (job picker → /jobs/{id}/artifacts → full content).
import * as api from "../api.js";
import { el, fmtNum, mount, shortId, timeAgo } from "../util.js";
import { emptyState, errorPanel, loading, statTile } from "../components.js";

const DOMAINS = ["", "prompt", "rag", "llm", "spec", "eng", "eng_design"];

export default function library(container) {
  let disposed = false;
  let tab = "gt";
  const body = el("div", {});

  const tabs = el(
    "div",
    { class: "row lib-tabs" },
    tabBtn("gt", "Ground truths"),
    tabBtn("artifacts", "Artifacts")
  );
  function tabBtn(id, label) {
    const b = el("button", { class: "btn btn-sm", text: label });
    b.addEventListener("click", () => {
      tab = id;
      tabs.querySelectorAll("button").forEach((x) => x.classList.remove("btn-primary"));
      b.classList.add("btn-primary");
      tab === "gt" ? renderGt() : renderArtifacts();
    });
    if (id === tab) b.classList.add("btn-primary");
    return b;
  }

  // ── GT browser ──────────────────────────────────────────────────────
  let gtPage = 1;
  const gtDomain = el(
    "select",
    { class: "input input-sm" },
    ...DOMAINS.map((d) => el("option", { value: d, text: d || "all domains" }))
  );
  gtDomain.addEventListener("change", () => {
    gtPage = 1;
    renderGt();
  });

  async function renderGt() {
    mount(body, loading("Loading corpus…"));
    try {
      const [stats, list] = await Promise.all([
        api.get("/gt/stats").catch(() => null),
        api.get("/gt/list", {
          query: { page: gtPage, per_page: 20, domain: gtDomain.value || null },
        }),
      ]);
      if (disposed || tab !== "gt") return;
      const entries = list.entries || [];
      const pager = el(
        "div",
        { class: "row lib-pager" },
        el("button", {
          class: "btn btn-sm btn-ghost", text: "← Prev", disabled: gtPage <= 1,
          onClick: () => { gtPage--; renderGt(); },
        }),
        el("span", { class: "faint", text: `page ${list.page} / ${list.total_pages}` }),
        el("button", {
          class: "btn btn-sm btn-ghost", text: "Next →",
          disabled: gtPage >= (list.total_pages || 1),
          onClick: () => { gtPage++; renderGt(); },
        })
      );
      mount(
        body,
        stats
          ? el(
              "div",
              { class: "row costs-tiles" },
              statTile("Entries", fmtNum(stats.total_entries)),
              ...Object.entries(stats.domains || {})
                .sort((a, b) => b[1] - a[1])
                .slice(0, 4)
                .map(([d, n]) => statTile(d, fmtNum(n)))
            )
          : null,
        el("div", { class: "row lib-controls" }, gtDomain, el("span", { class: "spacer" }), pager),
        ...entries.map((e) => gtRow(e))
      );
    } catch (e) {
      mount(body, errorPanel(e, renderGt));
    }
  }

  function gtRow(e) {
    const det = el(
      "details",
      { class: "card card-pad lib-row" },
      el(
        "summary",
        { class: "row lib-summary" },
        el("strong", { text: e.title || e.entry_id }),
        el("span", { class: "badge", text: e.domain }),
        el("span", { class: "faint", text: e.source_type || "" }),
        el("span", { class: "faint", text: `conf ${(e.confidence ?? 0).toFixed(2)}` })
      ),
      el("div", { class: "faint lib-snippet", text: e.snippet || "" })
    );
    let loaded = false;
    det.addEventListener("toggle", async () => {
      if (!det.open || loaded) return;
      loaded = true;
      const full = el("div", {}, loading("Loading entry…"));
      det.append(full);
      try {
        const d = await api.get(`/gt/detail/${encodeURIComponent(e.entry_id)}`);
        mount(full, el("pre", { class: "traces-pre", text: d.content || d.text || JSON.stringify(d, null, 2) }));
      } catch (err) {
        mount(full, el("div", { class: "faint", text: `detail unavailable: ${err.detail || err.message}` }));
      }
    });
    return det;
  }

  // ── Artifacts ───────────────────────────────────────────────────────
  async function renderArtifacts(jobId) {
    mount(body, loading("Loading…"));
    try {
      if (!jobId) {
        const res = await api.get("/jobs", { query: { limit: 25 } });
        const jobs = (res.jobs || []).filter((j) => j.status === "completed");
        if (disposed || tab !== "artifacts") return;
        if (!jobs.length) {
          mount(body, emptyState({ icon: "▤", title: "No completed jobs with artifacts yet" }));
          return;
        }
        mount(
          body,
          el("div", { class: "faint traces-hint", text: "Completed jobs — pick one:" }),
          ...jobs.map((j) =>
            el(
              "div",
              { class: "card card-pad traces-job-row" },
              el("a", {
                href: "#", text: j.title || shortId(j.id),
                onClick: (ev) => { ev.preventDefault(); renderArtifacts(j.id); },
              }),
              el("span", { class: "faint", text: timeAgo(j.completed_at || j.updated_at) })
            )
          )
        );
        return;
      }
      const res = await api.get(`/jobs/${jobId}/artifacts`);
      if (disposed || tab !== "artifacts") return;
      const arts = res.artifacts || [];
      const back = el("button", {
        class: "btn btn-sm btn-ghost", text: "← Jobs",
        onClick: () => renderArtifacts(),
      });
      if (!arts.length) {
        mount(body, back, emptyState({ icon: "▤", title: "No artifacts on this job", small: true }));
        return;
      }
      mount(
        body,
        back,
        ...arts.map((a) => {
          const det = el(
            "details",
            { class: "card card-pad lib-row" },
            el(
              "summary",
              { class: "row lib-summary" },
              el("strong", { text: a.filename || a.kind || shortId(a.id) }),
              el("span", { class: "badge", text: a.kind || "artifact" }),
              a.node_key ? el("span", { class: "badge", text: a.node_key }) : null,
              el("span", { class: "faint", text: `${fmtNum(a.size_bytes ?? a.content_length)} bytes` })
            )
          );
          let loaded = false;
          det.addEventListener("toggle", async () => {
            if (!det.open || loaded) return;
            loaded = true;
            const full = el("div", {}, loading("Loading artifact…"));
            det.append(full);
            try {
              const d = await api.get(`/artifacts/${a.id}`);
              mount(full, el("pre", { class: "traces-pre", text: (d.content || "").slice(0, 20000) }));
            } catch (err) {
              mount(full, el("div", { class: "faint", text: `unavailable: ${err.detail || err.message}` }));
            }
          });
          return det;
        })
      );
    } catch (e) {
      mount(body, errorPanel(e, () => renderArtifacts(jobId)));
    }
  }

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Library" }),
        el("div", {
          class: "sub",
          text: "The ground-truth corpus and per-job artifacts.",
        })
      ),
      tabs
    ),
    body
  );
  renderGt();

  return () => {
    disposed = true;
  };
}
