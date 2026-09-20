// Research explorer. Lists research sessions, shows a provenance verify audit,
// and runs new research live via POST /research (SSE) with the awaiting_reply
// pause/reply channel (POST /research/reply). Reuses the fetch SSE reader.
import * as api from "../api.js";

// §17.1120 — detached research runs. Exported for the node tests.
export const RUN_KEY = "scaffold_research_run";
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000];
const API_RUNS = "/research/runs";   // an API path, not a hash route (the route gate scans `return \`/…` lines)
export function runStreamPath(runId) { return API_RUNS + "/" + runId + "/stream"; }
/** After a reload: re-attach only to a run the engine says is still in flight. */
export function shouldReattach(status) { return !!(status && typeof status === "object" && status.running); }
/** Same decision table as the theater's stream drop (§17.1113). */
export function runDropDecision(status, attempt, maxAttempts) {
  if (status && typeof status === "object") return status.running ? "reattach" : "finished";
  return attempt >= maxAttempts ? "give_up" : "retry";
}

// §17.1121 — one sentence per research event, from the fields the agent
// ACTUALLY emits (app/modules/research_agent.py `_sse(...)` literals; the
// static gate tests/test_research_feed_wiring.py keeps the two in step).
// Before this, extraction/search/ingestion lines read "Extracted ? entries",
// "new ?, versioned ?, rejected ?", and every research_fetch / progress /
// extractor_fallback frame printed as its raw event name.
// Returns { text, cls, key } — `key` marks a line that is updated in place —
// or null for frames that are not feed lines (heartbeat, progress).
const RESEARCH_FEED_IGNORED = new Set(["heartbeat", "progress"]);
const n = (v) => (v == null ? "?" : v);
export function feedText(event, d) {
  d = d || {};
  switch (event) {
    case "research_started":
      return { text: `Started · ${d.topic || ""} (${shortId(d.session_id)})${d.depth ? ` · ${d.depth}` : ""}${d.mode && d.mode !== "topic" ? ` · ${d.mode}` : ""}` };
    case "research_resumed":
      return { text: `Resumed · ${d.topic || ""} (${shortId(d.session_id)}) — reply: ${d.reply || ""}` };
    case "decomposition_complete": {
      const facets = Array.isArray(d.facets) ? d.facets.length : "?";
      return { text: `Decomposed into ${facets} facets, ${n(d.query_count)} queries${d.complexity ? ` (${d.complexity})` : ""}` };
    }
    case "iteration_started":
      return { text: `Iteration ${n(d.iteration)} started${d.query_count != null ? ` — ${d.query_count} queries` : ""}` };
    case "iteration_complete":
      return { text: `Iteration ${n(d.iteration)} complete${d.entries_extracted != null ? ` — extracted ${d.entries_extracted}` : ""}${d.entries_ingested != null ? `, ingested ${d.entries_ingested}` : ""}${d.reason ? ` (${d.reason})` : ""}` };
    case "search_complete":
      return { text: `Search: ${n(d.results_found)} results (${n(d.total_urls)} URLs so far)${d.page_count != null ? ` · ${d.page_count} pages` : ""}${d.mode && d.mode !== "topic" ? ` · ${d.mode}` : ""}` };
    case "research_fetch":
      return { key: `fetch-${n(d.iteration)}`, text: `Fetching pages ${n(d.fetched)}/${n(d.total)} — ok ${n(d.ok)}, failed ${n(d.failed)}${d.last_url ? ` · ${d.last_url}` : ""}` };
    case "extraction_complete":
      return { text: `Extracted ${n(d.entries_extracted)} entries` };
    case "ingestion_complete": {
      const parts = [];
      if (d.new != null) parts.push(`new ${d.new}`);
      if (d.versioned != null) parts.push(`versioned ${d.versioned}`);
      if (d.rejected != null || d.total_rejected != null) parts.push(`rejected ${d.rejected ?? d.total_rejected}`);
      return { cls: "ok", text: `Ingested ${n(d.entries_ingested)} this iteration — ${n(d.total_ingested)} total${parts.length ? ` (${parts.join(", ")})` : ""}` };
    }
    case "contradictions_detected":
      return { cls: "warn", text: `Contradictions detected (${n(d.count)})` };
    case "gap_analysis":
      return { text: `Gap analysis — coverage ${d.coverage_pct != null ? Math.round(d.coverage_pct) + "%" : "?"}; gaps: ${(d.gap_facets || []).join(", ") || "none"}` };
    case "convergence":
      return { cls: "ok", text: `Converged — ${d.reason || d.message || ""}` };
    case "content_truncated":
      return { cls: "warn", text: `Content truncated to ${n(d.max_chars)} chars (${n(d.count)} entries, ${d.mode || "?"})` };
    case "distill_bypassed":
      return { text: `Distill bypassed for ${d.url || "?"} (${n(d.chunks)} chunks, ${d.source_type || "?"})` };
    case "extractor_fallback":
      return { cls: "warn", text: `Extractor fallback ${d.from || "?"} → ${d.to || "?"}${d.reason ? `: ${d.reason}` : ""}` };
    case "awaiting_reply":
      return { cls: "warn", text: `Awaiting reply: ${d.question || ""}` };
    case "research_complete":
      return { cls: "ok", text: `Complete — ${n(d.total_ingested)} entries ingested of ${n(d.total_entries)} in ${d.duration_ms != null ? (d.duration_ms / 1000).toFixed(1) + "s" : "?"} (${n(d.iterations)} iterations, ${n(d.total_urls_searched)} URLs)` };
    case "error":
      return { cls: "err", text: d.message || d.error || "Error" };
    case "warning":
      return { cls: "warn", text: `${d.message || "Warning"}${d.stage ? ` (${d.stage})` : ""}` };
    case "cache_hit_upstream":
      // §17.1133 — mode telemetry (forum/github/hf): upstream fetch cache counters
      return { text: `Upstream cache (${d.mode || "mode"}): ${fmtNum(d.hits || 0)} hits, ${fmtNum(d.misses || 0)} misses`, cls: "dim" };
    case "quality_gate_filtered": {
      const stats = Object.entries(d).filter(([k, v]) => typeof v === "number" && k !== "iteration").map(([k, v]) => `${k} ${fmtNum(v)}`);
      return { text: `Quality gate (${d.mode || "mode"}): ${stats.join(", ") || "no stats"}`, cls: "dim" };
    }
    case "source_ref_resolved":
      return { text: `Source ref (${d.mode || "mode"}): ${d.ref_hint || "default"} → ${d.resolved_ref || "?"}`, cls: "dim" };
    default:
      if (RESEARCH_FEED_IGNORED.has(event)) return null;
      return { text: `${event}${d.message ? " — " + d.message : ""}` };
  }
}

import { el, mount, shortId, timeAgo, fmtNum, mdToHtml } from "../util.js";
import { statusBadge, loading, errorPanel, toast, emptyState, makeClickable } from "../components.js";

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
  // §17.854 (audit G2) — a research stream is cancel-on-disconnect too; warn on
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
      el("div", {}, el("h1", { text: "Research Explorer" }), el("div", { class: "sub", text: "Autonomous research sessions & live runs — a live run keeps going if you leave this page; come back to watch it" })),
      el("div", { class: "header-actions" }, el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => loadSessions() }))
    ),
    runner,
    auditOutlet,
    el("div", { class: "section-head research-sec" }, el("h2", { text: "Sessions" })),
    listOutlet
  );

  function feedLine(ev, text, cls, key) {
    feed.classList.remove("hidden");
    // §17.1121 — a keyed line (fetch progress) is updated in place, not appended
    // five times per iteration.
    if (key) {
      const existing = feed.querySelector(`.log-line[data-key="${key}"]`);
      if (existing) { existing.querySelector(".log-txt").textContent = text; existing.className = `log-line ${cls || ""}`; return; }
    }
    const line = el("div", { class: `log-line ${cls || ""}`, dataset: key ? { key } : {} }, el("span", { class: "log-ico", text: RESEARCH_ICON[ev] || "·" }), el("span", { class: "log-txt", text }));
    feed.append(line);
    feed.scrollTop = feed.scrollHeight;
  }

  function setCoverage(pct) {
    if (pct == null) return;
    coverage.classList.remove("hidden");
    coverage.querySelector(".coverage-fill").style.width = `${Math.round(pct)}%`;
    coverage.querySelector(".coverage-label").textContent = `coverage ${Math.round(pct)}%`;
  }

  // §17.1120 — research runs are DETACHED now (POST /research/runs → run_id;
  // GET /research/runs/{id}/stream tails it). Closing the tab or navigating
  // away no longer cancels anything; ■ Stop is an explicit POST. A dropped
  // stream reconnects (backoff, backlog skip — the §17.1113 theater pattern),
  // and a reload re-attaches to the run id kept in sessionStorage.
  let activeRun = null;
  let framesSeen = 0;

  function toggleRun() {
    if (running) {
      if (!activeRun) { if (abort) abort.abort(); return; }
      if (!confirm("Stop this research run? What has been ingested so far is kept.")) return;
      runBtn.disabled = true;
      api.post(`/research/runs/${activeRun}/cancel`, {})
        .then(() => { feedLine("warning", "Stopped by operator.", "warn"); })
        .catch((e) => toast(`Could not stop the run: ${e.detail || e.message}`, "err"))
        .finally(() => { runBtn.disabled = false; if (abort) abort.abort(); });
      return;
    }
    const topic = topicInput.value.trim();
    if (!topic) { topicInput.focus(); return; }
    startRun(topic);
  }

  function armRunning() {
    running = true;
    runBtn.textContent = "■ Stop";
    runBtn.classList.replace("btn-primary", "btn-danger");
  }

  async function startRun(topic) {
    armRunning();
    activeSession = null;
    feed.replaceChildren();
    feed.classList.remove("hidden");
    summaryBox.classList.add("hidden");
    replyBox.classList.add("hidden");
    coverage.classList.add("hidden");
    const body = { topic, depth: depthSel.value };
    if (domainInput.value.trim()) body.domain = domainInput.value.trim();
    feedLine("research_started", `Starting ${depthSel.value} research… (keeps going if you leave this page — come back to watch)`);
    let res;
    try {
      res = await api.post("/research/runs", body);
    } catch (e) {
      feedLine("error", `Could not start research: ${e.detail || e.message}`, "err");
      finishRun();
      return;
    }
    rememberRun(res.run_id);
    await attachRun(res.run_id, { resume: false });
  }

  function rememberRun(id) {
    activeRun = id; framesSeen = 0;
    try { sessionStorage.setItem(RUN_KEY, id); } catch (e) { console.debug("research: run id not stored", e); }
  }

  async function attachRun(id, { resume = false } = {}) {
    armRunning();
    activeRun = id;
    if (!resume) framesSeen = 0;
    abort = new AbortController();
    let skip = resume ? framesSeen : 0;
    let terminal = false;
    try {
      for await (const { event, data } of api.stream(runStreamPath(id), { method: "GET", signal: abort.signal })) {
        if (disposed) break;
        if (skip > 0) { skip -= 1; continue; }
        framesSeen += 1;
        handleEvent(event, data || {});
        if (event === "research_complete" || event === "error") { terminal = true; break; }
      }
    } catch (e) {
      if (e.name === "AbortError") {
        console.debug("research: tail dropped on purpose (Stop or navigation); the run is untouched");
      } else if (!disposed) {
        feedLine("warning", `Connection lost (${e.message}) — the run continues server-side; reconnecting…`, "warn");
        const back = await reconnectRun(id);
        if (back) return;
      }
    }
    if (!disposed) finishRun();
  }

  async function reconnectRun(id) {
    for (let attempt = 1; attempt <= RECONNECT_DELAYS_MS.length; attempt++) {
      if (disposed || !running) return false;
      await new Promise((r) => setTimeout(r, RECONNECT_DELAYS_MS[attempt - 1]));
      let status = null;
      try { status = await api.get(`/research/runs/${id}`); } catch (e) { console.debug("research: status check failed", e); }
      const decision = runDropDecision(status, attempt, RECONNECT_DELAYS_MS.length);
      if (decision === "finished") {
        feedLine("research_complete", "The run ended while this page was disconnected — refreshing the sessions list.", "ok");
        return false;
      }
      if (decision === "reattach") {
        feedLine("research_resumed", `Reconnected (attempt ${attempt}) — resuming the live feed.`, "ok");
        attachRun(id, { resume: true });
        return true;
      }
      feedLine("warning", `Still can't reach the engine (attempt ${attempt}/${RECONNECT_DELAYS_MS.length})…`, "warn");
    }
    feedLine("error", "Could not reconnect. The run may still be going — reload this page to re-attach.", "err");
    return false;
  }

  function finishRun() {
    running = false;
    abort = null;
    activeRun = null;
    try { sessionStorage.removeItem(RUN_KEY); } catch (e) { console.debug("research: run id not cleared", e); }
    runBtn.textContent = "◎ Run research";
    runBtn.classList.replace("btn-danger", "btn-primary");
    loadSessions();
  }

  async function sendReply(text) {
    if (!activeSession) return;
    replyBox.classList.add("hidden");
    feedLine("research_resumed", `Replying: ${text}`);
    let res;
    try {
      res = await api.post("/research/runs/reply", { session_id: activeSession, reply: text });
    } catch (e) {
      feedLine("error", `Could not send the reply: ${e.detail || e.message}`, "err");
      return;
    }
    rememberRun(res.run_id);
    await attachRun(res.run_id, { resume: false });
  }

  function handleEvent(event, d) {
    if (d.session_id) activeSession = d.session_id;
    if (event === "gap_analysis") setCoverage(d.coverage_pct);
    if (event === "convergence" && d.coverage_pct != null) setCoverage(d.coverage_pct);
    if (event === "awaiting_reply") showReply(d.question || "The agent needs clarification.");
    if (event === "research_complete") showSummary(d);
    const r = feedText(event, d);
    if (r) feedLine(event, r.text, r.cls, r.key);
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
    makeClickable(tr, () => (location.hash = `#/research/${s.id}`),  // §17.854 G6
      { role: "link", label: `Open research ${s.topic || ""}` });
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
        el("button", { class: "btn btn-sm btn-ghost", text: "✕", "aria-label": "Close", onClick: () => (location.hash = "#/research") })
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

  // §17.1120 — re-attach to the run this tab was watching before a reload.
  try {
    const stored = sessionStorage.getItem(RUN_KEY);
    if (stored) {
      api.get(`/research/runs/${stored}`).then((st) => {
        if (disposed) return;
        if (shouldReattach(st)) { feed.classList.remove("hidden"); feedLine("research_resumed", "Re-attaching to the run you were watching…"); attachRun(stored, { resume: false }); }
        else { try { sessionStorage.removeItem(RUN_KEY); } catch (e) { console.debug("research: run id not cleared", e); } }
      }).catch((e) => console.debug("research: could not check the stored run", e));
    }
  } catch (e) { console.debug("research: sessionStorage unavailable", e); }

  return () => {
    disposed = true;
    if (abort) abort.abort();   // drop the tail only — the run is detached (§17.1120)
  };
}
