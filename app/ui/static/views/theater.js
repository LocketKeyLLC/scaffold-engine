// Live execution theater. Drives POST /execute/all (or /jobs/{id}/resume for
// cancelled jobs) via the fetch-based SSE reader and renders node lifecycle
// events live: node_start / node_token / node_done / node_retry / node_failed
// / pipeline_complete. node_token streaming is valve-gated (default OFF) — when
// absent we simply show each node's full output on node_done.
import * as api from "../api.js";
import { el, mount, shortId, mdToHtml, fmtNum } from "../util.js";
import { statusBadge, loading, errorPanel, makeClickable } from "../components.js";
import { flowGuide } from "./flow_guide.js";
import { isAssist, startAssistFor, onExecModeChange } from "../exec_mode.js";
import { toast } from "../components.js";
import * as notify from "../notify.js";

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

// ── Theater for one job ──────────────────────────────────────────────
// §17.859 — embedded as the job hub's Run tab (the picker + standalone
// route died with the hub). `ctx.setNavGuard(msg|null)` tells the hub to
// confirm before tab/back navigation while a run is streaming (G2: leaving
// the surface disconnects the SSE stream = cancel-by-design).
export function renderTheater(container, jobId, ctx = {}) {
  let disposed = false;
  let running = false;
  let abort = null;
  const nodeState = new Map(); // node_key -> {status, title, tool, output}
  let currentKey = null;

  const runBtn = el("button", { class: "btn btn-primary", text: isAssist() ? "✦ Start assist" : "▶ Run all", onClick: () => toggleRun() });
  // §17.854 (audit S4) — keep the run button honest if the mode toggles while
  // the theater is open (only when idle; a running label reads "■ Stop").
  const offExecMode = onExecModeChange(() => {
    if (!running) runBtn.textContent = isAssist() ? "✦ Start assist" : "▶ Run all";
  });
  const statusPill = el("span", {});

  // §17.854 (audit G2) — a live run dies with the SSE stream, so the hub is
  // told to confirm on tab/back navigation (ctx.setNavGuard) and the browser
  // warns on tab-close/reload.
  const GUARD_MSG = "A run is streaming. Leaving this page STOPS it. Leave anyway?";
  function beforeUnload(e) {
    if (running) { e.preventDefault(); e.returnValue = ""; return ""; }
  }

  const header = el(
    "div",
    { class: "row row-wrap theater-toolbar" },
    el("span", { class: "spacer" }),
    statusPill,
    runBtn
  );

  // §17.818 (plan 5.6) — the §17.811 progress/ETA signal, previously
  // emitted-but-unrendered here. Seeded from /exec/status (compute-on-read),
  // updated live by the `progress` SSE event.
  const progFill = el("div", { class: "prog-fill" });
  const progText = el("span", { class: "prog-text faint" });
  const progressBar = el("div", { class: "theater-progress hidden" },
    el("div", { class: "prog-track" }, progFill), progText);
  function setProgress(pr) {
    if (!pr || pr.total == null) return;
    progressBar.classList.remove("hidden");
    const pct = Math.max(0, Math.min(100, pr.pct ?? 0));
    progFill.style.width = pct + "%";
    progText.textContent =
      (pr.summary || `${pr.completed}/${pr.total} ${pr.unit || ""}`) +
      (pr.eta_human ? ` · ~${pr.eta_human} left` : "");
  }

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
  // §17.850 — flow guide on the Run surface too (carry-through sweep).
  const flowSlot = el("div", {});
  let jobTitle = ""; // §17.1007 — names the job in the notification, not a UUID
  api.get(`/jobs/${jobId}`).then((job) => {
    jobTitle = job.title || "";
    const fg = flowGuide(job, { here: `#/job/${jobId}/run` });
    if (fg) mount(flowSlot, fg);
  }).catch(() => {});
  mount(container, header, flowSlot, progressBar, grid);

  let lastJobStatus = null; // §17.818 — compare payload state, not DOM text
  function setStatusPill(status) {
    lastJobStatus = status;
    mount(statusPill, statusBadge(status));
  }

  // §17.1007 — call the operator back when a run ends while they are looking
  // elsewhere. announce() is a no-op on a focused tab (they watched it happen)
  // and dedupes per transition, so a reconnect cannot re-announce.
  function announceTerminal(label, body) {
    notify.announce({
      key: `${jobId}:theater:${label}`,
      count: 1,
      label,
      title: `${jobTitle || "Job"} — ${label}`,
      body,
      href: `#/job/${jobId}/run`,
    });
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
        if (n.status === "failed" && n.reason) row.title = n.reason; // §17.1007
        makeClickable(row, () => showNode(key),  // §17.854 G6
          { label: `View node ${key}` });
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
    // §17.1007 — on a failed node the REASON leads. The partial output that
    // tripped the verifier is still below it, but the operator's actual
    // question ("why did this stop?") is answered before they have to read it.
    const reasonPanel =
      n.status === "failed" && n.reason
        ? el("div", { class: "node-reason" },
            el("div", { class: "node-reason-label", text: "Why it failed" }),
            el("div", { class: "node-reason-text", text: n.reason }))
        : null;
    mount(
      stageBody,
      reasonPanel,
      n.output
        ? el("div", { class: "md", html: mdToHtml(n.output) })
        : el("div", { class: "dim", text: n.status === "running" ? "Running…" : "No output yet." })
    );
    renderNodes();
  }

  // §17.854 (audit G8) — coalesce streamed-token re-renders into one per
  // animation frame. The per-token path re-ran mdToHtml over the WHOLE
  // accumulated output and replaced stageBody on every delta (O(n²), and it
  // reset scroll + killed text selection mid-stream). rAF batching renders at
  // most ~60fps regardless of token rate.
  let _stageRenderPending = false;
  function scheduleStageRender(key) {
    if (key !== currentKey || _stageRenderPending) return;
    _stageRenderPending = true;
    requestAnimationFrame(() => {
      _stageRenderPending = false;
      const n = nodeState.get(currentKey);
      if (n && !disposed) mount(stageBody, el("div", { class: "md", html: mdToHtml(n.output || "") }));
    });
  }

  async function loadInitial() {
    try {
      const data = await api.get(`/exec/status/${jobId}`);
      if (disposed) return;
      setStatusPill(data.job_status);
      nodeState.clear();
      // §17.1007 — `failure_reason` has been in this exact payload since
      // §17.450 (execution_handler.py:153, from dag_nodes.last_verification_reason)
      // and the SPA read every OTHER field of it. The CLI renders it
      // (cli/scaffold_cli/main.py:3372); the console dropped it, so "why did
      // this fail" meant grepping Postgres.
      for (const n of data.nodes || [])
        nodeState.set(n.node_key, {
          status: n.status, title: n.title, tool: n.tool,
          order: n.execution_order, output: "", reason: n.failure_reason || "",
        });
      renderNodes();
      setProgress(data.progress);
      // §17.818 (plan 5.5) — one-shot auto-run handoff from the approve gate.
      if (sessionStorage.getItem("scaffold_autorun") === jobId) {
        sessionStorage.removeItem("scaffold_autorun");
        if (!running) toggleRun();
      }
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
    // §17.853 — the global mode gate: in Assist mode, Run means "start the
    // guided session", never autonomous /execute/all. Every path into the
    // theater's run action (button, approve auto-run intent) passes here.
    if (isAssist()) {
      startAssistFor(api, jobId, toast);
      return;
    }
    startRun();
  }

  async function startRun() {
    running = true;
    if (ctx.setNavGuard) ctx.setNavGuard(GUARD_MSG);  // §17.859 — hub tabs ask first
    window.addEventListener("beforeunload", beforeUnload);  // §17.854 G2
    summaryEl.classList.add("hidden");
    // §17.1007 — the contract, stated UP FRONT. Leaving this surface stops the
    // run (the SSE stream IS the run's liveness, §17.854 G2), and until that
    // changes server-side the operator deserves to know the deal before they
    // commit twenty minutes to it — not in the dialog that fires once they
    // have already tried to leave.
    log("warning", "Keep this tab open — closing it or leaving this page stops the run.", "warn");
    runBtn.textContent = "■ Stop";
    runBtn.classList.remove("btn-primary");
    runBtn.classList.add("btn-danger");
    abort = new AbortController();
    logEl.replaceChildren();
    log("queued", "Starting execution…");

    // cancelled jobs resume; everything else runs execute/all
    const cancelled = lastJobStatus === "cancelled";
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
    if (ctx.setNavGuard) ctx.setNavGuard(null);  // §17.859
    window.removeEventListener("beforeunload", beforeUnload);  // §17.854 G2
    abort = null;
    currentKey = null;
    runBtn.textContent = "▶ Run all";
    runBtn.classList.add("btn-primary");
    runBtn.classList.remove("btn-danger");
    renderNodes();
    // refresh authoritative status
    api.get(`/exec/status/${jobId}`).then((d) => !disposed && setStatusPill(d.job_status)).catch(() => {});
    // §17.1007 — and the flow guide with it: it was rendered once at mount, so
    // after a run it kept describing the pre-run state ("Plan ready, nothing
    // run yet") above a terminal success or failure card.
    api.get(`/jobs/${jobId}`).then((job) => {
      if (disposed) return;
      jobTitle = job.title || jobTitle;
      const fg = flowGuide(job, { here: `#/job/${jobId}/run` });
      if (fg) mount(flowSlot, fg);
    }).catch(() => {});
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
        scheduleStageRender(data.node_key);  // §17.854 G8 — rAF-coalesced
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
      case "progress":
        setProgress(data);
        break;
      case "node_retry":
        ensureNode(data.node_key, { status: "running" });
        log("node_retry", `${data.node_key} retry ${data.retry_count ?? ""} — ${data.message || ""}`, "warn");
        renderNodes();
        break;
      case "node_failed":
        // §17.1007 — keep the live reason, so a node that fails mid-stream
        // explains itself without waiting for a /exec/status refetch.
        ensureNode(data.node_key, { status: "failed", reason: data.error || data.message || "" });
        log("node_failed", `${data.node_key} failed — ${data.error || data.message || ""}`, "err");
        if (data.node_key === currentKey) showNode(data.node_key); // §17.1007 — parity with node_done
        renderNodes();
        break;
      case "budget_exhausted":
        log("budget_exhausted", `Budget exhausted — ${data.message || ""}`, "warn");
        break;
      case "awaiting_assist":
        log("awaiting_assist", "Parked — awaiting assist (human-in-the-loop).", "warn");
        announceTerminal("waiting on you", "The run parked and needs you to drive the next step."); // §17.1007
        break;
      case "pipeline_complete": {
        const nFailed = Number(data.failed || 0);
        // §17.1007 — a "complete" pipeline carrying failed nodes is a failure
        // the operator has to act on; give it the failure card, not the trophy.
        if (nFailed > 0) showFailure(data);
        else showSummary(data);
        log("pipeline_complete", `Complete — ${data.passed ?? "?"}/${data.total_nodes ?? "?"} passed`, nFailed ? "warn" : "ok");
        announceTerminal(
          nFailed ? "run finished with failures" : "run finished",
          nFailed
            ? `${nFailed} step${nFailed === 1 ? "" : "s"} failed. The Run tab has the reason and the recovery verbs.`
            : "The run completed. The compiled output is ready."
        );
        break;
      }
      case "execution_failed":
        log("execution_failed", `Execution failed — ${data.error || data.message || ""}`, "err");
        showFailure(data); // §17.1007 — endings get equal weight
        announceTerminal("run failed", data.error || data.message || "The run stopped. Open the Run tab for the reason.");
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
    // §17.1007 — "Pipeline complete" over 3 passed / 2 failed was the card
    // claiming a win the run did not have. The heading now reads the counts.
    const failed = Number(d.failed || 0);
    mount(
      summaryEl,
      el("div", { class: "card card-pad summary-card" + (failed ? " summary-partial" : "") },
        el("div", { class: "summary-title", text: failed ? `Finished with ${failed} failed step${failed === 1 ? "" : "s"}` : "Pipeline complete" }),
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

  // §17.1007 — a failed run used to end on a red line in a scrolling event log
  // while a successful one ended on a summary card. Endings are weighted
  // heavily in memory, and that asymmetry made every failure feel like an
  // abandonment. Failure now terminates with the same weight as success, and
  // carries the two things the operator actually needs: the reason, and a verb.
  function showFailure(d) {
    const failedKeys = [...nodeState.entries()].filter(([, v]) => v.status === "failed");
    const [firstKey, firstNode] = failedKeys[0] || [];
    const reason =
      (firstNode && firstNode.reason) || d.error || d.message || "No reason was recorded for this failure.";

    const retryBtn = firstKey
      ? el("button", { class: "btn btn-sm btn-primary", text: `↻ Retry ${firstKey}` })
      : null;
    if (retryBtn) {
      retryBtn.addEventListener("click", async () => {
        retryBtn.disabled = true;
        retryBtn.textContent = "Resetting…";
        try {
          await api.post("/exec/retry", { job_id: jobId, node_key: firstKey });
          toast(`${firstKey} reset to pending — press Run to pick it up.`, "ok");
          summaryEl.classList.add("hidden");
          await loadInitial();
        } catch (e) {
          toast(`Retry failed: ${e.detail || e.message}`, "err");
          retryBtn.disabled = false;
          retryBtn.textContent = `↻ Retry ${firstKey}`;
        }
      });
    }

    summaryEl.classList.remove("hidden");
    mount(
      summaryEl,
      el("div", { class: "card card-pad summary-card summary-failed" },
        el("div", { class: "summary-title", text: failedKeys.length > 1 ? `Run stopped — ${failedKeys.length} steps failed` : "Run stopped" }),
        firstKey
          ? el("div", { class: "summary-where" },
              el("span", { class: "mono", text: firstKey }),
              el("span", { text: ` · ${(firstNode && firstNode.title) || ""}` }))
          : null,
        el("div", { class: "node-reason" },
          el("div", { class: "node-reason-label", text: "Why it failed" }),
          el("div", { class: "node-reason-text", text: reason })),
        el("div", { class: "row row-wrap summary-actions" },
          retryBtn,
          el("a", { class: "btn btn-sm", href: `#/job/${jobId}/plan`, text: "Edit the step" }),
          firstKey
            ? el("button", {
                class: "btn btn-sm btn-ghost",
                text: "View its output",
                onClick: () => showNode(firstKey),
              })
            : null)
      )
    );
  }

  loadInitial();

  return () => {
    disposed = true;
    offExecMode();  // §17.854 S4
    if (ctx.setNavGuard) ctx.setNavGuard(null);  // §17.859
    window.removeEventListener("beforeunload", beforeUnload);  // §17.854 G2
    if (abort) abort.abort();
  };
}
