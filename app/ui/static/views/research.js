// Research explorer. Lists research sessions, shows a provenance verify audit,
// and runs new research live via POST /research (SSE) with the awaiting_reply
// pause/reply channel (POST /research/reply). Reuses the fetch SSE reader.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, fmtNum, mdToHtml } from "../util.js";
import { statusBadge, loading, errorPanel, toast, emptyState } from "../components.js";

const RESEARCH_ICON = {
  research_started: "◎",
  research_resumed: "◎",
  decomposition_complete: "❖",
  iteration_started: "▸",
  iteration_complete: "▪",
  search_complete: "🔎",
  extraction_complete: "✎",
  ingestion_complete: "⬇",
  contradictions_detected: "⚡",
  gap_analysis: "◑",
  convergence: "✓",
  awaiting_reply: "❓",
  research_complete: "★",
  error: "⚠",
  warning: "⚠",
};

export default function research(container, params) {
  let disposed = false;
  let running = false;
  let abort = null;
  let activeSession = null;

  // ── New-research runner ────────────────────────────────────────────
  const topicInput = el("input", { class: "input", placeholder: "Research topic, URL, github:owner/repo, or openapi:<url>" });
  const depthSel = el("select", { class: "input" }, el("option", { value: "shallow", text: "shallow" }), el("option", { value: "medium", text: "medium", selected: true }), el("option", { value: "deep", text: "deep" }));
  const domainInput = el("input", { class: "input", placeholder: "domain (optional, e.g. eng)" });
  const runBtn = el("button", { class: "btn btn-primary", text: "◎ Run research", onClick: () => toggleRun() });
  const feed = el("div", { class: "research-feed hidden" });
  const replyBox = el("div", { class: "research-reply hidden" });
  const summaryBox = el("div", { class: "research-summary hidden" });

  const coverage = el("div", { class: "coverage hidden" }, el("div", { class: "coverage-bar" }, el("div", { class: "coverage-fill" })), el("span", { class: "coverage-label faint" }));

  const runner = el(
    "div",
    { class: "card card-pad runner" },
    el("div", { class: "runner-grid" }, topicInput, depthSel, domainInput, runBtn),
    coverage,
    feed,
    replyBox,
    summaryBox
  );

  const listOutlet = el("div", { class: "research-list" }, loading("Loading sessions…"));
  const auditOutlet = el("div", {});

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el("div", {}, el("h1", { text: "Research Explorer" }), el("div", { class: "sub", text: "Autonomous research sessions & live runs" })),
      el("div", { class: "header-actions" }, el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => loadSessions() }))
    ),
    runner,
    auditOutlet,
    el("div", { class: "section-head research-sec" }, el("h2", { text: "Sessions" })),
    listOutlet
  );

  function feedLine(ev, text, cls) {
    feed.classList.remove("hidden");
    const line = el("div", { class: `log-line ${cls || ""}` }, el("span", { class: "log-ico", text: RESEARCH_ICON[ev] || "·" }), el("span", { class: "log-txt", text }));
    feed.append(line);
    feed.scrollTop = feed.scrollHeight;
  }

  function setCoverage(pct) {
    if (pct == null) return;
    coverage.classList.remove("hidden");
    coverage.querySelector(".coverage-fill").style.width = `${Math.round(pct)}%`;
    coverage.querySelector(".coverage-label").textContent = `coverage ${Math.round(pct)}%`;
  }

  function toggleRun() {
    if (running) {
      if (abort) abort.abort();
      return;
    }
    const topic = topicInput.value.trim();
    if (!topic) {
      toast("Enter a topic first.", "err");
      return;
    }
    startRun(topic);
  }

  async function startRun(topic) {
    running = true;
    activeSession = null;
    feed.replaceChildren();
    feed.classList.remove("hidden");
    summaryBox.classList.add("hidden");
    replyBox.classList.add("hidden");
    coverage.classList.add("hidden");
    runBtn.textContent = "■ Stop";
    runBtn.classList.replace("btn-primary", "btn-danger");
    abort = new AbortController();
    const body = { topic, depth: depthSel.value };
    if (domainInput.value.trim()) body.domain = domainInput.value.trim();
    feedLine("research_started", `Starting ${depthSel.value} research…`);
    try {
      for await (const { event, data } of api.stream("/research", { body, signal: abort.signal })) {
        if (disposed) break;
        handleEvent(event, data || {});
        if (event === "research_complete" || event === "error") break;
      }
    } catch (e) {
      if (e.name === "AbortError") feedLine("warning", "Stopped by operator.", "warn");
      else feedLine("error", `Stream error: ${e.message}`, "err");
    } finally {
      finishRun();
    }
  }

  function finishRun() {
    running = false;
    abort = null;
    runBtn.textContent = "◎ Run research";
    runBtn.classList.replace("btn-danger", "btn-primary");
    loadSessions();
  }

  async function sendReply(text) {
    if (!activeSession) return;
    replyBox.classList.add("hidden");
    running = true;
    runBtn.textContent = "■ Stop";
    runBtn.classList.replace("btn-primary", "btn-danger");
    abort = new AbortController();
    feedLine("research_resumed", `Replying: ${text}`);
    try {
      for await (const { event, data } of api.stream("/research/reply", { body: { session_id: activeSession, reply: text }, signal: abort.signal })) {
        if (disposed) break;
        handleEvent(event, data || {});
        if (event === "research_complete" || event === "error") break;
      }
    } catch (e) {
      feedLine("error", `Reply stream error: ${e.message}`, "err");
    } finally {
      finishRun();
    }
  }

  function handleEvent(event, d) {
    if (d.session_id) activeSession = d.session_id;
    switch (event) {
      case "research_started":
      case "research_resumed":
        feedLine(event, `${event === "research_resumed" ? "Resumed" : "Started"} · ${d.topic || ""} (${shortId(d.session_id)})`);
        break;
      case "decomposition_complete":
        feedLine(event, `Decomposed into ${d.facet_count ?? (d.facets || []).length ?? "?"} facets, ${d.query_count ?? (d.queries || []).length ?? "?"} queries`);
        break;
      case "iteration_started":
        feedLine(event, `Iteration ${d.iteration ?? "?"} started`);
        break;
      case "iteration_complete":
        feedLine(event, `Iteration ${d.iteration ?? "?"} complete`);
        break;
      case "search_complete":
        feedLine(event, `Search: ${d.results ?? d.result_count ?? "?"} results from ${d.queries ?? d.query_count ?? "?"} queries`);
        break;
      case "extraction_complete":
        feedLine(event, `Extracted ${d.extracted ?? d.count ?? "?"} entries`);
        break;
      case "ingestion_complete":
        feedLine(event, `Ingested — new ${d.new ?? "?"}, versioned ${d.versioned ?? "?"}, rejected ${d.rejected ?? "?"}`, "ok");
        break;
      case "contradictions_detected":
        feedLine(event, `Contradictions detected (${d.count ?? "?"})`, "warn");
        break;
      case "gap_analysis":
        setCoverage(d.coverage_pct);
        feedLine(event, `Gap analysis — coverage ${d.coverage_pct != null ? Math.round(d.coverage_pct) + "%" : "?"}; gaps: ${(d.gap_facets || []).join(", ") || "none"}`);
        break;
      case "convergence":
        feedLine(event, `Converged — ${d.reason || ""}`, "ok");
        break;
      case "awaiting_reply":
        showReply(d.question || "The agent needs clarification.");
        feedLine(event, `Awaiting reply: ${d.question || ""}`, "warn");
        break;
      case "research_complete":
        showSummary(d);
        feedLine(event, `Complete — ${d.total_ingested ?? d.total_entries ?? "?"} entries in ${d.duration_ms != null ? (d.duration_ms / 1000).toFixed(1) + "s" : "?"}`, "ok");
        break;
      case "heartbeat":
        break;
      case "error":
        feedLine("error", d.message || d.error || "Error", "err");
        break;
      case "warning":
        feedLine("warning", d.message || "Warning", "warn");
        break;
      default:
        feedLine(event, `${event}${d.message ? " — " + d.message : ""}`);
    }
  }

  function showReply(question) {
    replyBox.classList.remove("hidden");
    const input = el("input", { class: "input", placeholder: "Your reply…" });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && input.value.trim()) sendReply(input.value.trim());
    });
    mount(
      replyBox,
      el("div", { class: "reply-q" }, el("span", { class: "log-ico", text: "❓" }), el("span", { text: question })),
      el("div", { class: "row reply-row" }, input, el("button", { class: "btn btn-primary btn-sm", text: "Send", onClick: () => input.value.trim() && sendReply(input.value.trim()) }))
    );
    input.focus();
  }

  function showSummary(d) {
    summaryBox.classList.remove("hidden");
    const sources = d.sources || [];
    mount(
      summaryBox,
      el("div", { class: "summary-title", text: "Research complete" }),
      el(
        "div",
        { class: "summary-stats" },
        stat("Iterations", fmtNum(d.iterations)),
        stat("Entries", fmtNum(d.total_entries)),
        stat("Ingested", fmtNum(d.total_ingested ?? d.new)),
        stat("Sources", fmtNum(sources.length)),
        d.faithfulness != null ? stat("Faithfulness", Number(d.faithfulness).toFixed(2)) : null
      ),
      d.summary ? el("div", { class: "md research-summary-md", html: mdToHtml(d.summary) }) : null,
      sources.length
        ? el("div", { class: "src-list" }, el("div", { class: "drawer-label", text: "Sources" }), ...sources.slice(0, 12).map((s) => el("a", { class: "src-item", href: typeof s === "string" ? s : s.url || "#", target: "_blank", rel: "noopener", text: typeof s === "string" ? s : s.title || s.url || "source" })))
        : null
    );
  }
  function stat(k, v) {
    return el("div", { class: "sum-item" }, el("div", { class: "sum-v", text: String(v) }), el("div", { class: "sum-k", text: k }));
  }

  // ── Sessions list + verify audit ───────────────────────────────────
  async function loadSessions() {
    try {
      const res = await api.get("/research/sessions", { query: { limit: 50 } });
      if (disposed) return;
      const sessions = res.sessions || [];
      if (!sessions.length) {
        mount(listOutlet, emptyState({
          icon: "◎",
          title: "No research yet",
          body: "Research a topic, URL, GitHub repo, or OpenAPI spec — sessions you run appear here.",
          action: {
            label: "Start research",
            onClick: () => { topicInput.focus(); topicInput.scrollIntoView({ block: "center" }); },
          },
        }));
        return;
      }
      mount(
        listOutlet,
        el(
          "div",
          { class: "card table-wrap" },
          el(
            "table",
            { class: "table" },
            el("thead", {}, el("tr", {}, ["Status", "Topic", "Depth", "Domain", "Iters", "Ingested", "Coverage", "Updated"].map((h) => el("th", { text: h })))),
            el("tbody", {}, ...sessions.map(sessionRow))
          )
        )
      );
    } catch (e) {
      if (!disposed) mount(listOutlet, errorPanel(e, () => loadSessions()));
    }
  }

  function sessionRow(s) {
    const tr = el(
      "tr",
      {},
      el("td", {}, statusBadge(s.status)),
      el("td", { class: "recent-title", text: s.topic || "(untitled)" }),
      el("td", { class: "mono", text: s.depth || "—" }),
      el("td", { class: "mono", text: s.domain || "—" }),
      el("td", { class: "mono", text: String(s.iterations_completed ?? "—") }),
      el("td", { class: "mono", text: String(s.total_entries_ingested ?? "—") }),
      el("td", { class: "mono", text: s.coverage_pct != null ? Math.round(s.coverage_pct) + "%" : "—" }),
      el("td", { class: "faint", text: timeAgo(s.updated_at || s.created_at) })
    );
    tr.addEventListener("click", () => (location.hash = `#/research/${s.id}`));
    return tr;
  }

  async function loadAudit(sessionId) {
    mount(auditOutlet, el("div", { class: "card card-pad" }, loading("Loading provenance audit…")));
    try {
      const a = await api.get(`/research/verify/${sessionId}`);
      if (disposed) return;
      const t = a.totals || {};
      const meta = a.session_meta || {};
      const entries = a.entries || [];
      const head = el("div", { class: "row" },
        el("h2", { class: "audit-title", text: `Provenance audit · ${meta.topic || shortId(sessionId)}` }),
        el("span", { class: "spacer" }),
        statusBadge(meta.status),
        el("button", { class: "btn btn-sm btn-ghost", text: "✕", onClick: () => (location.hash = "#/research") })
      );
      const stats = el("div", { class: "summary-stats audit-stats" },
        stat("Provenance rows", fmtNum(t.provenance_rows)),
        stat("In Milvus", fmtNum(t.in_milvus)),
        stat("Superseded", fmtNum(t.superseded)),
        stat("Missing", fmtNum(t.missing))
      );
      let body;
      if (entries.length) {
        const rows = entries.slice(0, 40).map((e) =>
          el("tr", {},
            el("td", { text: e.title || "—" }),
            el("td", { class: "mono", text: e.in_milvus ? "✓" : "✕" }),
            el("td", { class: "mono faint", text: (e.source || e.url || "").slice(0, 48) })
          )
        );
        const thead = el("thead", {}, el("tr", {}, ["Title", "In Milvus", "Source"].map((h) => el("th", { text: h }))));
        body = el("div", { class: "table-wrap audit-entries" }, el("table", { class: "table" }, thead, el("tbody", {}, ...rows)));
      } else {
        body = el("div", { class: "dim audit-empty", text: "No provenance entries recorded for this session." });
      }
      mount(auditOutlet, el("div", { class: "card card-pad audit-card" }, head, stats, body));
    } catch (e) {
      if (!disposed) mount(auditOutlet, errorPanel(e));
    }
  }

  loadSessions();
  if (params && params.sessionId) loadAudit(params.sessionId);

  return () => {
    disposed = true;
    if (abort) abort.abort();
  };
}
