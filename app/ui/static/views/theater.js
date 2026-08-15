// Live execution theater. Drives POST /execute/all (or /jobs/{id}/resume for
// cancelled jobs) via the fetch-based SSE reader and renders node lifecycle
// events live: node_start / node_token / node_done / node_retry / node_failed
// / pipeline_complete. node_token streaming is valve-gated (default OFF) — when
// absent we simply show each node's full output on node_done.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, mdToHtml, fmtNum } from "../util.js";
import { statusBadge, loading, errorPanel } from "../components.js";

const TERMINAL = new Set(["pipeline_complete", "execution_failed", "error", "budget_exhausted", "awaiting_assist"]);

function eventIcon(ev) {
  return (
    {
      queued: "•",
      dag_generated: "⬡",
      node_start: "▶",
      node_done: "✓",
      node_retry: "↻",
      node_failed: "✕",
      pipeline_complete: "★",
      execution_failed: "✕",
      awaiting_assist: "✦",
      budget_exhausted: "$",
      error: "⚠",
      warning: "⚠",
    }[ev] || "·"
  );
}

// ── Picker ────────────────────────────────────────────────────────────
function renderPicker(container) {
  let disposed = false;
  const outlet = el("div", { class: "picker-outlet" }, loading("Loading jobs…"));
  mount(
    container,
    el("div", { class: "view-header" }, el("div", {}, el("h1", { text: "Execution Theater" }), el("div", { class: "sub", text: "Pick a job to run and watch live" }))),
    outlet
  );
  (async () => {
    try {
      const res = await api.get("/jobs", { query: { limit: 100 } });
      if (disposed) return;
      const jobs = (res.jobs || []).filter((j) => (j.node_count || 0) > 0);
      if (!jobs.length) {
        mount(outlet, el("div", { class: "card empty-state" }, el("div", { class: "empty-icon", text: "▶" }), el("p", { text: "No runnable jobs (none have a DAG yet)." })));
        return;
      }
      mount(
        outlet,
        el(
          "div",
          { class: "grid grid-3" },
          ...jobs.map((j) =>
            el(
              "a",
              { class: "card card-pad picker-card", href: `#/theater/${j.id}` },
              el("div", { class: "row row-wrap" }, statusBadge(j.status), el("span", { class: "spacer" }), el("span", { class: "faint mono", text: `${j.node_count} nodes` })),
              el("div", { class: "work-title", text: j.title || "(untitled)" }),
              el("div", { class: "faint", text: timeAgo(j.updated_at || j.created_at) })
            )
          )
        )
      );
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e));
    }
  })();
  return () => (disposed = true);
}

// ── Theater for one job ──────────────────────────────────────────────
function renderTheater(container, jobId) {
  let disposed = false;
  let running = false;
  let abort = null;
  const nodeState = new Map(); // node_key -> {status, title, tool, output}
  let currentKey = null;

  const runBtn = el("button", { class: "btn btn-primary", text: "▶ Run all", onClick: () => toggleRun() });
  const statusPill = el("span", {});
  const header = el(
    "div",
    { class: "view-header" },
    el("div", {}, el("h1", { text: "Execution Theater" }), el("div", { class: "sub mono", text: shortId(jobId) })),
    el(
      "div",
      { class: "header-actions" },
      el("a", { class: "btn btn-sm btn-ghost", href: "#/theater", text: "← Jobs" }),
      el("a", { class: "btn btn-sm btn-ghost", href: `#/dag/${jobId}`, text: "⬡ DAG" }),
      statusPill,
      runBtn
    )
  );

  const nodeListEl = el("div", { class: "theater-nodes" }, loading("Loading nodes…"));
  const stageTitle = el("div", { class: "stage-node-title dim", text: "Idle — press Run to begin." });
  const stageBody = el("div", { class: "stage-body md" });
  const logEl = el("div", { class: "theater-log" });
  const summaryEl = el("div", { class: "theater-summary hidden" });

  const grid = el(
    "div",
    { class: "theater-grid" },
    el("div", { class: "card theater-panel nodes-panel" }, el("div", { class: "panel-head", text: "Nodes" }), nodeListEl),
    el(
      "div",
      { class: "theater-center" },
      summaryEl,
      el("div", { class: "card theater-panel stage-panel" }, el("div", { class: "panel-head" }, stageTitle), stageBody),
      el("div", { class: "card theater-panel log-panel" }, el("div", { class: "panel-head", text: "Event stream" }), logEl)
    )
  );
  mount(container, header, grid);

  function setStatusPill(status) {
    mount(statusPill, statusBadge(status));
  }

  function log(ev, text, cls) {
    const line = el("div", { class: `log-line ${cls || ""}` }, el("span", { class: "log-ico", text: eventIcon(ev) }), el("span", { class: "log-txt", text }));
    logEl.append(line);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function renderNodes() {
    const items = [...nodeState.entries()].sort((a, b) => (a[1].order ?? 0) - (b[1].order ?? 0));
    mount(
      nodeListEl,
      ...items.map(([key, n]) => {
        const row = el(
          "div",
          { class: `theater-node st-${n.status}${key === currentKey ? " current" : ""}` },
          el("span", { class: "tn-key mono", text: key }),
          el("span", { class: "tn-title", text: n.title || "" }),
          statusBadge(n.status)
        );
        row.addEventListener("click", () => showNode(key));
        return row;
      })
    );
  }

  function showNode(key) {
    const n = nodeState.get(key);
    if (!n) return;
    currentKey = key;
    stageTitle.classList.remove("dim");
    mount(stageTitle, el("span", { class: "mono", text: key }), el("span", { text: " · " + (n.title || "") }), statusBadge(n.status));
    mount(stageBody, n.output ? el("div", { class: "md", html: mdToHtml(n.output) }) : el("div", { class: "dim", text: n.status === "running" ? "Running…" : "No output yet." }));
    renderNodes();
  }

  async function loadInitial() {
    try {
      const data = await api.get(`/exec/status/${jobId}`);
      if (disposed) return;
      header.querySelector(".sub").textContent = data.job_title || shortId(jobId);
      setStatusPill(data.job_status);
      nodeState.clear();
      for (const n of data.nodes || []) nodeState.set(n.node_key, { status: n.status, title: n.title, tool: n.tool, order: n.execution_order, output: "" });
      renderNodes();
      // Adjust the run button for cancelled jobs
      if (data.job_status === "cancelled") runBtn.textContent = "▶ Resume";
      const counts = data.counts || {};
      if (!counts.pending && !counts.running) {
        runBtn.textContent = "▶ Re-run pending";
      }
    } catch (e) {
      if (!disposed) mount(nodeListEl, errorPanel(e, () => loadInitial()));
    }
  }

  function toggleRun() {
    if (running) {
      if (abort) abort.abort();
      return;
    }
    startRun();
  }

  async function startRun() {
    running = true;
    summaryEl.classList.add("hidden");
    runBtn.textContent = "■ Stop";
    runBtn.classList.remove("btn-primary");
    runBtn.classList.add("btn-danger");
    abort = new AbortController();
    logEl.replaceChildren();
    log("queued", "Starting execution…");

    // cancelled jobs resume; everything else runs execute/all
    const cancelled = statusPill.textContent.trim() === "cancelled";
    const path = cancelled ? `/jobs/${jobId}/resume` : "/execute/all";
    const body = cancelled ? {} : { job_id: jobId };

    try {
      for await (const { event, data } of api.stream(path, { body, signal: abort.signal })) {
        if (disposed) break;
        handleEvent(event, data);
        if (TERMINAL.has(event)) break;
      }
    } catch (e) {
      if (e.name === "AbortError") log("warning", "Stopped by operator.", "warn");
      else log("error", `Stream error: ${e.message}`, "err");
    } finally {
      finishRun();
    }
  }

  function finishRun() {
    running = false;
    abort = null;
    currentKey = null;
    runBtn.textContent = "▶ Run all";
    runBtn.classList.add("btn-primary");
    runBtn.classList.remove("btn-danger");
    renderNodes();
    // refresh authoritative status
    api.get(`/exec/status/${jobId}`).then((d) => !disposed && setStatusPill(d.job_status)).catch(() => {});
  }

  function ensureNode(key, patch) {
    const cur = nodeState.get(key) || { status: "pending", title: "", output: "", order: nodeState.size };
    nodeState.set(key, { ...cur, ...patch });
  }

  function handleEvent(event, data) {
    data = data || {};
    switch (event) {
      case "queued":
        log("queued", `Queued (job ${shortId(data.job_id || jobId)})`);
        break;
      case "dag_generated":
        log("dag_generated", `DAG generated (${data.node_count ?? "?"} nodes)`);
        loadInitial();
        break;
      case "node_start":
        currentKey = data.node_key;
        ensureNode(data.node_key, { status: "running", title: data.title, tool: data.tool });
        log("node_start", `${data.node_key} · ${data.title || ""}  [${data.tool || "LLM"}]`);
        showNode(data.node_key);
        break;
      case "node_token": {
        const n = nodeState.get(data.node_key) || {};
        ensureNode(data.node_key, { output: (n.output || "") + (data.delta || "") });
        if (data.node_key === currentKey) mount(stageBody, el("div", { class: "md", html: mdToHtml(nodeState.get(data.node_key).output) }));
        break;
      }
      case "node_done":
        ensureNode(data.node_key, {
          status: "done",
          title: data.title,
          tool: data.tool,
          output: data.output || nodeState.get(data.node_key)?.output || "",
        });
        log("node_done", `${data.node_key} done${data.verified === false ? " (unverified)" : ""}${data.confidence != null ? ` · conf ${Number(data.confidence).toFixed(2)}` : ""}`, "ok");
        if (data.node_key === currentKey) showNode(data.node_key);
        renderNodes();
        break;
      case "node_retry":
        ensureNode(data.node_key, { status: "running" });
        log("node_retry", `${data.node_key} retry ${data.retry_count ?? ""} — ${data.message || ""}`, "warn");
        renderNodes();
        break;
      case "node_failed":
        ensureNode(data.node_key, { status: "failed" });
        log("node_failed", `${data.node_key} failed — ${data.error || data.message || ""}`, "err");
        renderNodes();
        break;
      case "budget_exhausted":
        log("budget_exhausted", `Budget exhausted — ${data.message || ""}`, "warn");
        break;
      case "awaiting_assist":
        log("awaiting_assist", "Parked — awaiting assist (human-in-the-loop).", "warn");
        break;
      case "pipeline_complete":
        showSummary(data);
        log("pipeline_complete", `Complete — ${data.passed ?? "?"}/${data.total_nodes ?? "?"} passed`, "ok");
        break;
      case "execution_failed":
        log("execution_failed", `Execution failed — ${data.error || data.message || ""}`, "err");
        break;
      case "error":
        log("error", data.message || data.error || "Error", "err");
        break;
      case "warning":
        log("warning", data.message || "Warning", "warn");
        break;
      case "heartbeat":
        break;
      default:
        log(event, `${event}${data.message ? " — " + data.message : ""}`);
    }
  }

  function showSummary(d) {
    summaryEl.classList.remove("hidden");
    mount(
      summaryEl,
      el("div", { class: "card card-pad summary-card" },
        el("div", { class: "summary-title", text: "Pipeline complete" }),
        el("div", { class: "summary-stats" },
          stat("Status", d.status || "completed"),
          stat("Nodes", fmtNum(d.total_nodes)),
          stat("Passed", fmtNum(d.passed)),
          stat("Failed", fmtNum(d.failed)),
          stat("Duration", d.duration_ms != null ? `${(d.duration_ms / 1000).toFixed(1)}s` : "—")
        )
      )
    );
  }
  function stat(k, v) {
    return el("div", { class: "sum-item" }, el("div", { class: "sum-v", text: String(v) }), el("div", { class: "sum-k", text: k }));
  }

  loadInitial();

  return () => {
    disposed = true;
    if (abort) abort.abort();
  };
}

export default function theater(container, params) {
  if (params && params.jobId) return renderTheater(container, params.jobId);
  return renderPicker(container);
}
