// Live execution theater. Drives POST /execute/all (or /jobs/{id}/resume for
// cancelled jobs) via the fetch-based SSE reader and renders node lifecycle
// events live: node_start / node_token / node_done / node_retry / node_failed
// / pipeline_complete. node_token streaming is valve-gated (default OFF) — when
// absent we simply show each node's full output on node_done.
import * as api from "../api.js";
import { jobStore } from "../store.js";
import { el, mount, shortId, mdToHtml, fmtNum, timeAgo } from "../util.js";
import { statusBadge, loading, errorPanel, makeClickable, nextActionChips } from "../components.js";
import { flowGuide } from "./flow_guide.js";
import { isAssist, startAssistFor, onExecModeChange } from "../exec_mode.js";
import { toast } from "../components.js";
import * as notify from "../notify.js";

// §17.1113 (ledger U-3) — reconnect policy for a dropped run stream. Exported
// for the node tests. A dropped stream is not a finished run: the run is
// detached (§17.1007) and keeps going; the tab re-attaches and skips the
// frames it already showed.
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000];

/**
 * What to do after a stream drop, given the fresh /exec/status (or null when
 * that call failed too): "reattach" while the run is live, "finished" when
 * the engine says nothing is running, "retry" while unreachable and attempts
 * remain, "give_up" on the last failed attempt.
 */
export function streamDropDecision(status, attempt, maxAttempts) {
  if (status && typeof status === "object") {
    return status.detached_running ? "reattach" : "finished";
  }
  return attempt >= maxAttempts ? "give_up" : "retry";
}

/** Frames to render from a replayed backlog after `seen` were already shown. */
export function framesAfter(frames, seen) {
  return Array.isArray(frames) ? frames.slice(Math.max(0, seen | 0)) : [];
}

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

  // §17.1007 — the §17.854 G2 nav guard and beforeunload warning are GONE,
  // because the thing they warned about no longer happens: the run is a
  // detached background task (app/modules/run_broker.py) and this stream is
  // only a subscriber to it. Closing the tab drops the subscriber. Warning an
  // operator away from a door that is no longer a trapdoor is worse than not
  // warning them — it teaches them the console's warnings are noise.

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
  jobStore.get(jobId).then((job) => {
    jobTitle = job.title || "";
    const fg = flowGuide(job, { here: `#/job/${jobId}/run` });
    if (fg) mount(flowSlot, fg);
  }).catch(() => {});
  mount(container, header, flowSlot, progressBar, grid);

  let lastJobStatus = null; // §17.818 — compare payload state, not DOM text
  function setStatusPill(status, actions) {
    lastJobStatus = status;
    // §17.1134 — /exec/status carries next_actions (retry/skip/resume …); render them
    // beside the pill instead of leaving the recovery vocabulary on the wire (ledger D-6)
    mount(statusPill, statusBadge(status), nextActionChips(actions, { jobId, limit: 3 }));
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
          // §17.1135 — the fields /exec/status carries and nothing rendered (ledger D-7):
          // a runnable pending node, and when a node started / finished
          n.is_deliverable ? el("span", { class: "tag", title: "Deliverable node — its output is part of the result", text: "★" }) : null,
          n.assigned_model ? el("span", { class: "tag mono", title: "Model assigned to this node", text: String(n.assigned_model).split(":")[0] }) : null,
          n.actionable && n.status === "pending" ? el("span", { class: "tag", title: "Dependencies met — runs next", text: "ready" }) : null,
          !n.actionable && n.status === "pending" && (n.depends_on || []).length
            ? el("span", { class: "faint", title: "Waits on these nodes", text: `waits on ${(n.depends_on || []).slice(0, 3).join(", ")}${(n.depends_on || []).length > 3 ? "…" : ""}` }) : null,
          n.started_at && !n.completed_at ? el("span", { class: "faint", text: `started ${timeAgo(n.started_at)}` }) : null,
          n.completed_at ? el("span", { class: "faint", text: `done ${timeAgo(n.completed_at)}` }) : null,
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
    // §17.1040 — the executor's evidence report (§17.1039). What the node
    // stated that nothing it was given supports leads; the output is below.
    const evidencePanel = evidenceLines(n.evidence).length
      ? el("div", { class: "node-reason node-evidence" },
          el("div", { class: "node-reason-label", text: "Evidence check" }),
          ...evidenceLines(n.evidence).map((t) => el("div", { class: "node-reason-text", text: t })))
      : null;
    mount(
      stageBody,
      reasonPanel,
      evidencePanel,
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

  // §17.1009 — render the persisted event log of a finished/interrupted run.
  // Deliberately quiet on an empty result: an empty list means "no record"
  // (Redis down, or the 24h log expired), never "nothing happened", so it must
  // not be rendered as an empty-but-authoritative stream.
  async function replayPastRun() {
    try {
      const { frames } = await api.get(`/exec/events/${jobId}`);
      if (!Array.isArray(frames) || !frames.length || disposed) return;
      log("queued", `Replaying ${frames.length} event(s) from this run — it is not live.`, "warn");
      for (const frame of frames) {
        const ev = /^event:\s*(\S+)/m.exec(frame);
        const dm = /^data:\s*(.*)$/m.exec(frame);
        if (!ev) continue;
        let payload = {};
        try { payload = dm ? JSON.parse(dm[1]) : {}; } catch { /* keep {} */ }
        const key = payload.node_key ? `${payload.node_key} · ` : "";
        log(ev[1], `${key}${payload.title || payload.message || payload.error || ev[1]}`,
            ev[1].includes("fail") || ev[1] === "error" ? "err" : "");
      }
    } catch (e) {
      // §17.1117 (ledger U-9) — replay is a nicety and must never break the
      // Run tab, but a failed read is not an empty run: say which it was.
      log("warning", `Could not load this run's event log (${(e && (e.detail || e.message)) || "unknown error"}) — the node states above are still authoritative.`, "warn");
    }
  }

  async function loadInitial() {
    try {
      const data = await api.get(`/exec/status/${jobId}`);
      if (disposed) return;
      setStatusPill(data.job_status, data.next_actions);
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
          evidence: n.evidence || null,  // §17.1040
        });
      renderNodes();
      setProgress(data.progress);
      // §17.1007 — a run is in flight for this job RIGHT NOW: attach to it.
      //
      // This is the payoff of detaching. Open the Run tab on a job that is
      // already executing and you now see it live, whether you started it, or
      // closed the tab twenty minutes ago, or are on a different machine.
      //
      // Gated on `detached_running`, NOT on job_status === "running": after a
      // restart the row still says running while no task exists, and attaching
      // there would silently START execution — an action nobody asked for on
      // page load. When that is the case we offer the verb instead (below).
      if (data.detached_running && !running) {
        log("queued", "Attaching to the run already in progress…", "ok");
        attachRun();
      } else if (data.job_status === "running" && !running) {
        // Row says running, no live task — the process restarted mid-run.
        // Say so honestly and let the operator decide to pick it up.
        log("warning",
          "This job is marked running but nothing is executing — the engine restarted mid-run. Press ▶ to carry on with the remaining steps.",
          "warn");
      }
      // §17.1009 — replay what a run that is no longer in flight actually did.
      //
      // A detached run dies with its process and takes its in-memory frame
      // buffer with it, so a job the engine restarted under showed a status of
      // failed and an EMPTY event stream — no account of which steps ran or
      // why the last one stopped. Frames are mirrored to Redis as they are
      // produced; this reads them back when there is nothing live to stream.
      if (!data.detached_running && !running && !logEl.childElementCount) {
        replayPastRun();
      }
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

  async function toggleRun() {
    if (running) {
      // §17.1007 — stopping used to mean "disconnect and let the server infer
      // it". Now that a disconnect is just a detach, stopping has to say so.
      if (!confirm("Stop this run? Completed steps are kept; the rest stay pending.")) return;
      runBtn.disabled = true;
      runBtn.textContent = "Stopping…";
      try {
        await api.post(`/jobs/${jobId}/cancel`, {});
        toast("Run stopped.", "ok");
      } catch (e) {
        toast(`Could not stop the run: ${e.detail || e.message}`, "err");
      } finally {
        runBtn.disabled = false;
        if (abort) abort.abort(); // drop our subscriber; the run is already cancelled
      }
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

  // §17.1007 — attaching and starting issue the SAME request: run_broker.start()
  // returns the in-flight run when there is one. The client does not need to
  // know which happened, and must not race to guess.
  function attachRun() { return startRun({ attach: true }); }

  // §17.1113 (Phase 1 ledger U-3) — frames this tab has already rendered from
  // the current run. The broker replays its whole backlog to every new
  // subscriber, so a re-attach after a dropped stream skips this many.
  let framesSeen = 0;

  async function startRun({ attach = false, resume = false } = {}) {
    running = true;
    summaryEl.classList.add("hidden");
    if (!resume) {
      // §17.1007 — the contract, stated up front, and it is now the good one:
      // the run outlives this tab. (An earlier pass in this same change told the
      // operator the opposite, which was true right up until the run was
      // detached server-side.)
      log("queued", "This run keeps going if you close the tab — reopen it any time to watch. ⚑ Alerts will tell you when it ends.", "ok");
      framesSeen = 0;
    }
    runBtn.textContent = "■ Stop";
    runBtn.classList.remove("btn-primary");
    runBtn.classList.add("btn-danger");
    abort = new AbortController();
    if (!attach && !resume) logEl.replaceChildren();
    if (!resume) log("queued", attach ? "Attached — streaming live events." : "Starting execution…");

    // cancelled jobs resume; everything else runs execute/all
    const cancelled = lastJobStatus === "cancelled";
    const path = cancelled ? `/jobs/${jobId}/resume` : "/execute/all";
    const body = cancelled ? {} : { job_id: jobId };

    let skip = resume ? framesSeen : 0;
    try {
      for await (const { event, data } of api.stream(path, { body, signal: abort.signal })) {
        if (disposed) break;
        if (skip > 0) { skip -= 1; continue; }      // already rendered before the drop
        framesSeen += 1;
        handleEvent(event, data);
        if (TERMINAL.has(event)) break;
      }
    } catch (e) {
      if (e.name === "AbortError") {
        log("warning", "Stopped by operator.", "warn");
      } else if (!disposed) {
        // §17.1113 — a dropped stream is NOT a finished run: the run is
        // detached and keeps going server-side. Before this, the catch fell
        // into finishRun(), which reset the button to "▶ Run all" beside a
        // status pill that said running. Now: reconnect with backoff.
        log("warning", `Connection lost (${e.message}) — the run continues server-side; reconnecting…`, "warn");
        const back = await reconnectRun();
        if (back) return;          // the resumed stream owns finishRun now
      }
    }
    // terminal event, operator stop, run ended while disconnected, or every
    // reconnect attempt failed — the run is over for this tab.
    if (!disposed) finishRun();
  }

  // §17.1113 — try to re-attach to the in-flight run. Returns true when a
  // resumed stream took over (it will call finishRun itself); false when the
  // run is no longer live or every attempt failed (the caller finishes).
  async function reconnectRun() {
    for (let attempt = 1; attempt <= RECONNECT_DELAYS_MS.length; attempt++) {
      if (disposed || !running) return false;
      await new Promise((r) => setTimeout(r, RECONNECT_DELAYS_MS[attempt - 1]));
      let status = null;
      try { status = await api.get(`/exec/status/${jobId}`); } catch { /* still unreachable */ }
      const decision = streamDropDecision(status, attempt, RECONNECT_DELAYS_MS.length);
      if (decision === "finished") {
        log("queued", "The run ended while this tab was disconnected — showing its final state.", "ok");
        return false;
      }
      if (decision === "reattach") {
        log("queued", `Reconnected (attempt ${attempt}) — resuming the live stream.`, "ok");
        startRun({ attach: true, resume: true });   // not awaited: it owns the button now
        return true;
      }
      log("warning", `Still can't reach the engine (attempt ${attempt}/${RECONNECT_DELAYS_MS.length})…`, "warn");
    }
    log("error", "Could not reconnect. The run may still be going server-side — reload this tab to re-attach.", "err");
    return false;
  }

  function finishRun() {
    running = false;
    abort = null;
    currentKey = null;
    runBtn.textContent = "▶ Run all";
    runBtn.classList.add("btn-primary");
    runBtn.classList.remove("btn-danger");
    renderNodes();
    // refresh authoritative status — and tell the hub header (§17.1114,
    // ledger U-7): its pill was rendered once from the job row and listens for
    // `scaffold:job-status`, which only the assist view dispatched, so an
    // autonomous run finished under a header that still said "running".
    api.get(`/exec/status/${jobId}`).then((d) => {
      if (disposed) return;
      setStatusPill(d.job_status, d.next_actions);
      window.dispatchEvent(new CustomEvent("scaffold:job-status", { detail: { jobId, status: d.job_status } }));
    }).catch(() => {});
    // §17.1007 — and the flow guide with it: it was rendered once at mount, so
    // after a run it kept describing the pre-run state ("Plan ready, nothing
    // run yet") above a terminal success or failure card.
    jobStore.get(jobId, { fresh: true }).then((job) => {   // the run just changed it
      if (disposed) return;
      jobTitle = job.title || jobTitle;
      const fg = flowGuide(job, { here: `#/job/${jobId}/run` });
      if (fg) mount(flowSlot, fg);
    }).catch(() => {});
  }

  // §17.1040 — one line per finding in the evidence report; empty when clean.
  function evidenceLines(ev) {
    if (!ev || !ev.checked) return [];
    const out = [];
    if (ev.unsupported && ev.unsupported.length)
      out.push("⚠️ Unverified specifics — appear in no source, fact or note for this step; confirm before relying on them: " + ev.unsupported.join(", "));
    if (ev.plan_only && ev.plan_only.length)
      out.push("ℹ️ From the plan, not confirmed on your system: " + ev.plan_only.join(", "));
    if (ev.command_shape && ev.command_shape.length)
      out.push("⚠️ Command mismatch — an interpreter paired with a file it cannot run: " + ev.command_shape.join(", "));
    if (ev.citation_score != null && ev.citation_score < 0.6)
      out.push(`⚠️ Weak sourcing — only ${Math.round(ev.citation_score * 100)}% of cited statements are backed by the source they cite.`);
    if (ev.regenerated && !out.length)
      out.push("✓ First draft stated values from memory; regenerated against the sources.");
    return out;
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
          evidence: data.evidence || nodeState.get(data.node_key)?.evidence || null,  // §17.1040
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
    // §17.1007 — aborting here detaches THIS subscriber. The run continues.
    if (abort) abort.abort();
  };
}
