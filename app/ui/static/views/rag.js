// §17.816 (plan 5.4b) — RAG view: retrieval search with the uncertainty flags
// the audit found hidden from operators (below_threshold / degraded /
// keyword_backend / skipped_rerank), plus the dedup log. Ingestion happens via
// Research (topic/url/pdf) and GT extraction — this view links there instead
// of pretending a direct-ingest endpoint exists.
import * as api from "../api.js";
import { el, mount } from "../util.js";
import { emptyState, errorPanel, loading, toast } from "../components.js";

export default function rag(container) {
  let disposed = false;

  const query = el("input", {
    class: "input rag-query",
    placeholder: "Search the knowledge base…",
  });
  const domain = el(
    "select",
    { class: "input input-sm" },
    el("option", { value: "", text: "all domains" })
  );
  api.domains().then((ds) => ds.forEach((d) => domain.append(el("option", { value: d, text: d }))));
  const history = el("input", { type: "checkbox" });
  const searchBtn = el("button", { class: "btn btn-primary", text: "Search" });
  const resultsBox = el("div", { class: "rag-results" });

  async function search() {
    const q = query.value.trim();
    if (!q) {
      toast("Enter a query first.", "err");
      return;
    }
    searchBtn.disabled = true;
    mount(resultsBox, loading("Searching… (rerank can take ~10–20 s on CPU)"));
    try {
      const res = await api.post("/rag", {
        query: q,
        top_k: 10,
        domain: domain.value || null,
        include_history: history.checked,
      });
      renderResults(res);
    } catch (e) {
      mount(resultsBox, errorPanel(e, search));
    } finally {
      searchBtn.disabled = false;
    }
  }

  function flagBadges(res) {
    // §17.812 Phase-2 work made these honest — surface them, don't bury them.
    const meta = res.metadata || {};
    const flags = [];
    if (res.below_threshold) flags.push(["below threshold — low confidence", "warn"]);
    if (res.degraded || meta.degraded) flags.push(["degraded — partitions failed", "err"]);
    if (meta.skipped_rerank || res.skipped_rerank) flags.push(["rerank skipped", "warn"]);
    if (meta.reranker_backend && /rrf/i.test(String(meta.reranker_backend)))
      flags.push(["RRF fallback (reranker down)", "err"]);
    if (meta.keyword_backend && /like/i.test(String(meta.keyword_backend)))
      flags.push(["keyword: LIKE fallback (BM25 unavailable)", "warn"]);
    (res.warnings || []).forEach((w) => {
      if (w !== "below_threshold") flags.push([String(w), "warn"]);
    });
    return flags.map(([text, cls]) => el("span", { class: `badge ${cls}`, text }));
  }

  function renderResults(res) {
    if (disposed) return;
    const items = res.results || [];
    if (!items.length) {
      mount(
        resultsBox,
        el("div", { class: "row rag-flags" }, ...flagBadges(res)),
        emptyState({
          icon: "◎",
          title: "No results",
          body: "Nothing crossed the confidence threshold. Ingest more via Research or GT extraction.",
        })
      );
      return;
    }
    mount(
      resultsBox,
      el("div", { class: "row rag-flags" }, ...flagBadges(res)),
      ...items.map((r, i) =>
        el(
          "div",
          { class: "card card-pad rag-hit" },
          el(
            "div",
            { class: "row rag-hit-head" },
            el("strong", { text: `#${i + 1}` }),
            el("span", { class: "badge", text: `score ${Number(r.scores?.final ?? r.scores?.rerank ?? 0).toFixed(3)}` }),
            r.domain ? el("span", { class: "badge", text: r.domain }) : null,
            r.version > 1 ? el("span", { class: "badge", text: `v${r.version}` }) : null,
            r.source_type ? el("span", { class: "faint", text: r.source_type }) : null,
            r.source_url
              ? el("a", { class: "faint", href: r.source_url, target: "_blank", rel: "noopener", text: "source" })
              : null
          ),
          r.title ? el("div", { class: "rag-hit-title", text: r.title }) : null,
          el("div", { class: "rag-hit-text", text: (r.content || "").slice(0, 1200) })
        )
      )
    );
  }

  async function loadDedup(box) {
    try {
      const res = await api.get("/rag/dedup", { query: { limit: 10 } });
      const rows = res.entries || res.items || [];
      if (!rows.length) {
        mount(box, el("div", { class: "faint", text: "No recent dedup decisions." }));
        return;
      }
      mount(
        box,
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {},
            el("th", { text: "When" }), el("th", { text: "Action" }),
            el("th", { text: "Similarity" }), el("th", { text: "Snippet" }))),
          el("tbody", {}, ...rows.map((d) =>
            el("tr", {},
              el("td", { class: "faint", text: (d.created_at || "").slice(0, 19) }),
              el("td", {}, el("span", { class: "badge", text: d.action || d.decision || "?" })),
              el("td", { text: d.similarity != null ? Number(d.similarity).toFixed(3) : "—" }),
              el("td", { class: "faint", text: (d.text || d.snippet || "").slice(0, 90) }))
          ))
        )
      );
    } catch (e) {
      mount(box, el("div", { class: "faint", text: `dedup log unavailable: ${e.detail || e.message}` }));
    }
  }

  searchBtn.addEventListener("click", search);
  query.addEventListener("keydown", (e) => {
    if (e.key === "Enter") search();
  });

  const dedupBox = el("div", {}, loading("Loading dedup log…"));
  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "Knowledge (RAG)" }),
        el("div", {
          class: "sub",
          text: "Search the corpus with honest uncertainty flags. Ingest new knowledge via Research (topic / url / pdf) or GT extraction.",
        })
      )
    ),
    el(
      "div",
      { class: "card card-pad" },
      el("div", { class: "row rag-controls" }, query, domain,
        el("label", { class: "row faint rag-history" }, history, " include superseded history"),
        searchBtn)
    ),
    resultsBox,
    el("h2", { class: "rag-dedup-title", text: "Recent dedup decisions" }),
    dedupBox
  );
  loadDedup(dedupBox);
  query.focus();

  return () => {
    disposed = true;
  };
}
