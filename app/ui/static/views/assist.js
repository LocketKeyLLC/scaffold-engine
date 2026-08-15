// Assistant chat. Renders the assist session as a chat: transcript from
// /assist/{id}/turns (operator vs assistant bubbles, markdown), a context
// sidebar (step counts, current node, notes, memory facts), and a live
// step-guidance driver via /assist/{id}/guide/stream (SSE: assist_guide_delta
// / assist_guide_done). Message composer persists via /assist/{id}/turn.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, fmtDate, mdToHtml } from "../util.js";
import { statusBadge, loading, errorPanel, toast } from "../components.js";

// ── Picker ────────────────────────────────────────────────────────────
function renderPicker(container) {
  let disposed = false;
  const outlet = el("div", { class: "picker-outlet" }, loading("Loading assist sessions…"));
  mount(
    container,
    el("div", { class: "view-header" }, el("div", {}, el("h1", { text: "Assistant" }), el("div", { class: "sub", text: "Human-in-the-loop assist sessions" }))),
    outlet
  );
  (async () => {
    try {
      const [work, cand] = await Promise.all([api.get("/work"), api.get("/assist/candidates").catch(() => ({ candidates: [] }))]);
      if (disposed) return;
      const sessions = work.assist_sessions || [];
      const startable = (cand.candidates || []).filter((c) => c.status === "awaiting_assist");
      const blocks = [];
      if (sessions.length) {
        blocks.push(el("div", { class: "section-head" }, el("h2", { text: "Active sessions" }), el("span", { class: "count-pill", text: String(sessions.length) })));
        blocks.push(
          el(
            "div",
            { class: "grid grid-3" },
            ...sessions.map((s) =>
              el(
                "a",
                { class: "card card-pad picker-card", href: `#/assist/${s.session_id}` },
                el("div", { class: "row row-wrap" }, statusBadge(s.status), el("span", { class: "spacer" }), s.current_node_key ? el("span", { class: "tag", text: s.current_node_key }) : null),
                el("div", { class: "work-title", text: s.job_title || "(untitled)" }),
                el("div", { class: "faint", text: s.last_activity_at ? timeAgo(s.last_activity_at) : "no activity yet" })
              )
            )
          )
        );
      }
      if (startable.length) {
        blocks.push(el("div", { class: "section-head assist-startable" }, el("h2", { text: "Awaiting assist" }), el("span", { class: "count-pill", text: String(startable.length) })));
        blocks.push(
          el(
            "div",
            { class: "grid grid-3" },
            ...startable.map((c) => {
              const card = el(
                "div",
                { class: "card card-pad picker-card startable" },
                el("div", { class: "row row-wrap" }, statusBadge(c.status), el("span", { class: "spacer" }), el("span", { class: "faint mono", text: `${c.node_count} nodes` })),
                el("div", { class: "work-title", text: c.title || "(untitled)" }),
                el("button", { class: "btn btn-sm btn-primary", text: "Start assist", onClick: () => startAssist(c.job_id) })
              );
              return card;
            })
          )
        );
      }
      if (!blocks.length) {
        mount(outlet, el("div", { class: "card empty-state" }, el("div", { class: "empty-icon", text: "✦" }), el("p", { text: "No assist sessions. Park a job as a plan (awaiting_assist) to drive it here." })));
        return;
      }
      mount(outlet, ...blocks);
    } catch (e) {
      if (!disposed) mount(outlet, errorPanel(e));
    }
  })();

  async function startAssist(jobId) {
    try {
      const s = await api.post("/assist/start", { job_id: jobId });
      const sid = s.id || s.session_id;
      if (sid) location.hash = `#/assist/${sid}`;
      else toast("Started, but no session id returned.", "err");
    } catch (e) {
      toast(`Start failed: ${e.detail || e.message}`, "err");
    }
  }

  return () => (disposed = true);
}

// ── Chat for one session ─────────────────────────────────────────────
function renderChat(container, sessionId) {
  let disposed = false;
  let guiding = false;
  let abort = null;
  let session = null;
  let turns = [];

  const transcript = el("div", { class: "chat-transcript" }, loading("Loading conversation…"));
  const sidebar = el("aside", { class: "chat-sidebar" });
  const composerText = el("textarea", { class: "chat-input", placeholder: "Message the assistant…", rows: "2" });
  const guideBtn = el("button", { class: "btn btn-sm", text: "✦ Guide current step", onClick: () => guideCurrent() });
  const sendBtn = el("button", { class: "btn btn-sm btn-primary", text: "Send", onClick: () => sendMessage() });
  const composer = el(
    "div",
    { class: "chat-composer" },
    composerText,
    el("div", { class: "composer-actions" }, guideBtn, el("span", { class: "spacer" }), sendBtn)
  );

  const header = el(
    "div",
    { class: "view-header" },
    el("div", {}, el("h1", { text: "Assistant" }), el("div", { class: "sub mono", text: shortId(sessionId) })),
    el("div", { class: "header-actions" }, el("a", { class: "btn btn-sm btn-ghost", href: "#/assist", text: "← Sessions" }), el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => load() }))
  );

  const main = el("div", { class: "chat-main" }, transcript, composer);
  mount(container, header, el("div", { class: "chat-grid" }, main, sidebar));

  composerText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      sendMessage();
    }
  });

  function bubble(role, kind, content, ts) {
    const cls = role === "operator" ? "op" : role === "assistant" ? "as" : "sys";
    return el(
      "div",
      { class: `msg ${cls}` },
      el("div", { class: "msg-meta" }, el("span", { class: "msg-role", text: role }), kind && kind !== "message" ? el("span", { class: "msg-kind", text: kind }) : null, el("span", { class: "msg-time faint", text: ts ? timeAgo(ts) : "" })),
      el("div", { class: "msg-body md", html: mdToHtml(content || "") })
    );
  }

  function renderTranscript() {
    if (!turns.length) {
      mount(transcript, el("div", { class: "empty-state small" }, el("p", { text: "No messages yet." })));
      return;
    }
    mount(transcript, ...turns.map((t) => bubble(t.role, t.kind, t.content, t.created_at)));
    transcript.scrollTop = transcript.scrollHeight;
  }

  function renderSidebar() {
    if (!session) return;
    const sc = session.step_counts || {};
    const notes = (session.notes || []).slice(-6).reverse();
    const facts = (session.memory_facts || []).slice(-8).reverse();
    mount(
      sidebar,
      el("div", { class: "card card-pad side-block" },
        el("div", { class: "side-title", text: session.job_title || "Session" }),
        el("div", { class: "row row-wrap side-badges" }, statusBadge(session.status), session.current_node_key ? el("span", { class: "tag", text: "node " + session.current_node_key } ) : null),
        row("Handoff", session.handoff_policy),
        row("Replan", session.replan_policy),
        row("Divergence", String(session.divergence_count ?? 0)),
        row("Started", fmtDate(session.started_at))
      ),
      el("div", { class: "card card-pad side-block" },
        el("div", { class: "side-title", text: "Steps" }),
        el("div", { class: "step-counts" }, ...Object.entries(sc).map(([k, v]) => el("span", { class: "strip-item" }, el("span", { class: "tag", text: k }), el("span", { class: "strip-n mono", text: String(v) }))))
      ),
      notes.length ? el("div", { class: "card card-pad side-block" },
        el("div", { class: "side-title", text: "Recent notes" }),
        ...notes.map((n) => el("div", { class: "side-note" }, el("span", { class: "note-kind tag", text: n.kind || "note" }), el("span", { class: "note-text", text: n.text || "" })))
      ) : null,
      facts.length ? el("div", { class: "card card-pad side-block" },
        el("div", { class: "side-title", text: "Memory facts" }),
        ...facts.map((f) => el("div", { class: "side-fact", text: typeof f === "string" ? f : f.text || JSON.stringify(f) }))
      ) : null
    );
  }
  function row(k, v) {
    return el("div", { class: "meta-row" }, el("span", { class: "meta-k", text: k }), el("span", { class: "meta-v", text: v ?? "—" }));
  }

  async function load() {
    try {
      const [s, t] = await Promise.all([api.get(`/assist/${sessionId}`), api.get(`/assist/${sessionId}/turns`)]);
      if (disposed) return;
      session = s;
      turns = t.turns || [];
      header.querySelector(".sub").textContent = s.job_title || shortId(sessionId);
      renderTranscript();
      renderSidebar();
    } catch (e) {
      if (!disposed) mount(transcript, errorPanel(e, () => load()));
    }
  }

  function historyForGuide() {
    return turns.slice(-8).map((t) => ({ role: t.role, content: t.content }));
  }

  async function sendMessage() {
    const text = composerText.value.trim();
    if (!text || guiding) return;
    composerText.value = "";
    turns.push({ role: "operator", kind: "message", content: text, created_at: new Date().toISOString() });
    renderTranscript();
    try {
      await api.post(`/assist/${sessionId}/turn`, { role: "operator", kind: "message", content: text, node_key: session?.current_node_key || null });
    } catch (e) {
      toast(`Could not persist message: ${e.detail || e.message}`, "err");
    }
    // Ask the assistant to respond via a fresh step-guidance stream.
    guideCurrent(text);
  }

  async function guideCurrent(userMsg) {
    if (guiding) {
      if (abort) abort.abort();
      return;
    }
    guiding = true;
    guideBtn.textContent = "■ Stop";
    abort = new AbortController();
    const history = historyForGuide();
    if (userMsg) history.push({ role: "operator", content: userMsg });
    // live assistant bubble that grows with deltas
    const bodyEl = el("div", { class: "msg-body md" }, el("span", { class: "spin" }));
    const live = el("div", { class: "msg as streaming" }, el("div", { class: "msg-meta" }, el("span", { class: "msg-role", text: "assistant" }), el("span", { class: "msg-kind", text: "guiding" })), bodyEl);
    transcript.append(live);
    transcript.scrollTop = transcript.scrollHeight;
    let acc = "";
    try {
      for await (const { event, data } of api.stream(`/assist/${sessionId}/guide/stream`, {
        body: { node_key: session?.current_node_key || null, force: true, history },
        signal: abort.signal,
      })) {
        if (disposed) break;
        if (event === "assist_guide_delta") {
          acc += (data && data.text) || "";
          bodyEl.innerHTML = mdToHtml(acc);
          transcript.scrollTop = transcript.scrollHeight;
        } else if (event === "assist_guide_done") {
          if (data && data.guidance && !acc) bodyEl.innerHTML = mdToHtml(data.guidance);
          live.classList.remove("streaming");
          break;
        } else if (event === "error") {
          bodyEl.innerHTML = mdToHtml(`⚠ ${(data && (data.message || data.error)) || "guidance error"}`);
          break;
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") bodyEl.innerHTML = mdToHtml(`⚠ Stream error: ${e.message}`);
    } finally {
      guiding = false;
      abort = null;
      guideBtn.textContent = "✦ Guide current step";
      live.classList.remove("streaming");
      // reload authoritative turns (guidance is persisted server-side)
      load();
    }
  }

  load();

  return () => {
    disposed = true;
    if (abort) abort.abort();
  };
}

export default function assist(container, params) {
  if (params && params.sessionId) return renderChat(container, params.sessionId);
  return renderPicker(container);
}
