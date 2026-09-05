// Assistant chat. Renders the assist session as a chat: transcript from
// /assist/{id}/turns (operator vs assistant bubbles, markdown), a context
// sidebar (step counts, current node, notes, memory facts), and a live
// step-guidance driver via /assist/{id}/guide/stream (SSE: assist_guide_delta
// / assist_guide_done). Message composer persists via /assist/{id}/turn.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, fmtDate, mdToHtml, stickyScroll, selectionWithin } from "../util.js";
import { statusBadge, loading, errorPanel, toast, emptyState } from "../components.js";
import { briefPanel } from "./brief_panel.js";

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
        mount(outlet, emptyState({
          icon: "✦",
          title: "No assist sessions",
          body: "Park a job as a plan (status: awaiting_assist) to drive it step-by-step here with the assistant.",
          action: { label: "＋ New idea", href: "#/new" },
        }));
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

// How-assist-works onboarding (research: new users need the human/AI contract
// stated up front — the #1 confusion is "does it control my machine?").
// Dismissed once per browser.
const ASSIST_ONBOARD_KEY = "scaffold_assist_onboarded";
function contractCard(onDismiss) {
  if (localStorage.getItem(ASSIST_ONBOARD_KEY)) return null;
  const step = (n, t, b) =>
    el("div", { class: "welcome-step" },
      el("div", { class: "welcome-step-n", text: String(n) }),
      el("div", {}, el("div", { class: "welcome-step-t", text: t }), el("div", { class: "welcome-step-b dim", text: b })));
  return el(
    "div",
    { class: "card card-pad assist-contract" },
    el("div", { class: "row" },
      el("h3", { class: "brief-heading", text: "How assist mode works" }),
      el("span", { class: "spacer" }),
      el("button", { class: "btn btn-ghost btn-sm", text: "Got it", onClick: (e) => { localStorage.setItem(ASSIST_ONBOARD_KEY, "1"); e.target.closest(".assist-contract")?.remove(); onDismiss?.(); } })),
    el("p", { class: "assist-contract-lede", text: "The engine never touches your machine — it has no terminal access, by design. You are its hands: it guides, you act on your computer, it tracks and adapts." }),
    el("div", { class: "welcome-steps" },
      step(1, "Guide", "Press ✦ Guide me — the assistant walks you through the current step with exact commands or clicks for YOUR environment."),
      step(2, "Do it", "Run the commands in your own terminal (or click through the UI it names). Copy-paste is expected."),
      step(3, "Report back", "Paste what happened — output, errors, screenshots described in words. Errors? Use Fix error for a diagnosis."),
      step(4, "Advance", "✓ Submit records the result and moves to the next step. The engine verifies, remembers, and re-plans around what you tell it."))
  );
}

// §17.859 — exported: the job hub's Run tab embeds the walkthrough for
// assisted_* jobs (it resolves the session via the idempotent /assist/start).
export function renderChat(container, sessionId) {
  let disposed = false;
  let guiding = false;
  let abort = null;
  let session = null;
  let turns = [];
  // §17.870 — current-turn output rendered live but not (yet) in the durable
  // transcript; renderTranscript re-appends it after every reconcile.
  // §17.871 — turnStartedAt: only a durable turn captured DURING this turn may
  // replace an ephemeral entry (see renderTranscript).
  let ephemeralTail = [];
  let turnStartedAt = null;
  // §17.929 — the operator's OWN messages, held on screen until a durable turn
  // with the same text comes back from the server. `ephemeralTail` only ever
  // protected ASSISTANT output, so the operator's half of the conversation had
  // no such guard: sendMessage() pushed an optimistic bubble into `turns`, and
  // the end-of-turn load() overwrote `turns` wholesale with the server list.
  // Whenever the operator's line was not in that response — the §17.928 window
  // bug put every message past turn 200 outside it, and a slow/failed capture
  // does the same — what they had just typed silently disappeared mid-session.
  // Nothing told them whether it had been sent, ignored, or lost.
  let pendingOps = [];

  const transcript = el("div", { class: "chat-transcript" }, loading("Loading conversation…"));
  // §17.890 — scroll only while pinned to the bottom; defer transcript
  // re-renders while the operator holds a text selection in it (right-click
  // copy needs the selected nodes to survive until the menu's Copy runs).
  const stick = stickyScroll(transcript);
  let transcriptRenderDeferred = false;
  const onSelChange = () => {
    if (transcriptRenderDeferred && !selectionWithin(transcript)) renderTranscript();
  };
  document.addEventListener("selectionchange", onSelChange);
  // Operator layout: no right sidebar. The input checklist docks LEFT of the
  // chat input; session/steps/notes/facts become a card row BELOW the chat.
  const checklistPanel = el("div", { class: "composer-checklist hidden" });
  const belowGrid = el("div", { class: "assist-below grid grid-3" });
  const composerText = el("textarea", {
    class: "chat-input",
    placeholder: "Paste terminal output or describe what happened — or ask anything…",
    rows: "2",
  });
  const guideBtn = el("button", { class: "btn btn-sm", text: "✦ Guide me", onClick: () => guideCurrent() });
  const sendBtn = el("button", { class: "btn btn-sm btn-primary", text: "Send", onClick: () => sendMessage() });
  // Current-step hero — the persistent status layer over the conversation.
  const stepHero = el("div", { class: "card card-pad step-hero hidden" });
  // §17.863 — pending re-plan proposal card slot (declared HERE, above the
  // chrome mount that references it — const does not hoist).
  const replanSlot = el("div", { class: "replan-slot" });
  // §17.816 (plan 5.4h) — step verbs, previously slash/OWUI-only. Submit and
  // Fix consume the composer text (evidence / error report); the server side
  // captures + derives it (§17.812 3E) and verifies submits (§17.731).
  const verbBtns = {};
  function verb(label, title, fn) {
    const b = el("button", { class: "btn btn-sm btn-ghost", text: label, title });
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        await fn();
      } catch (e) {
        toast(e.detail || e.message, "err");
      } finally {
        b.disabled = false;
      }
    });
    verbBtns[label] = b;
    return b;
  }
  // §17.848 — advance to the next claimable step and stream its walkthrough.
  async function claimAndGuideNext() {
    // §17.868 — claim + premise check + guidance are ONE server-side stream
    // now; the server sends a status frame for every stage (including the
    // §17.864 stale-premise warning), so nothing here sequences anything.
    await load();
    await runTurnStream({ command: "guide", node_key: null });
  }

  // §17.848 — evidence-in-box submit with honest per-outcome handling
  // (§17.847). Returns true when the loop advanced or ended cleanly.
  async function submitEvidence(nk, output, opts = {}) {
    composerText.value = "";
    // §17.890 — quiet: the operator's message is already in the transcript
    // (typed-claim fall-through from doneNext); don't echo it twice.
    if (!opts.quiet) appendBubble("operator", "submit", output);
    const res = await api.post(`/assist/${sessionId}/submit`, {
      node_key: nk, output, action: "submit", history: historyForGuide(),
    });
    const st = res?.status;
    if (st === "committed") {
      toast(`✓ Step ${nk} committed.`, "ok");
      await load();
      if (session?.status !== "completed") await claimAndGuideNext();
      return true;
    }
    if (st === "verification_failed" || st === "step_incomplete") {
      const v = res.success_verdict || {};
      appendBubble("assistant", "verify",
        `⚠ Not committed — the verifier judged this step **${st === "step_incomplete" ? "incomplete" : "not successful"}**.` +
        (v.reason || v.summary ? `\n\n${v.reason || v.summary}` : "") +
        `\n\nAdd more evidence and press ✓ again, use 🔧 Fix error if something failed, or ⏩ Skip to move on anyway.`);
      return false;
    }
    if (st === "deliberating") {
      appendBubble("assistant", "decision", (res.decision_message || res.message) || "This step needs your input — see the question above and answer in the box.");
      load();
      return false;
    }
    if (st === "auto_handoff") {
      toast(`Step ${nk} handed to the engine (policy: ${res.handoff_policy}).`, "ok");
      load();
      return true;
    }
    toast(`Step ${nk} submitted (${st || "recorded"}).`, "ok");
    load();
    return true;
  }

  // §17.848 — THE advance verb. Operator hit a three-way dead end trying to
  // move on (Submit demanded text, Next re-presented the same step, typed
  // "next step" only got guidance). One button now does the right thing:
  //   box has text  → submit it as evidence (verified, honest outcomes);
  //   box is empty  → the §17.754 progress tracker assesses the CONVERSATION
  //                   as the evidence — confident-done retires the step and
  //                   the next one streams; not-done gets an honest bubble
  //                   (never a "type something first" error).
  // §17.901 — undo the last ✓ Done (or ⏩ Skip) and put the operator back on
  // that step. Deliberately does NOT re-run the guide: the stored walkthrough
  // is re-rendered as-is, because a regeneration would hand them different
  // instructions for work they were part-way through.
  async function stepBack() {
    try {
      const res = await api.post(`/assist/${sessionId}/step/back`, {});
      const r = res?.reopened || {};
      await load();
      appendBubble("assistant", "track",
        `↩ Reopened **${r.node_key}: ${r.title}** — you're back on it, with the same ` +
        `walkthrough you had. Nothing else in the plan moved.`);
      // Render the PRESERVED walkthrough the server handed back. Deliberately
      // NOT guideCurrent(): that runs the guide pipeline, and the reopen bumps
      // dag_nodes.updated_at, which trips §17.894's staleness probe and would
      // regenerate — landing the operator on a different walkthrough for work
      // they were part-way through.
      if (r.guidance) appendBubble("assistant", "guide", r.guidance);
      toast(`Back on ${r.node_key}.`, "ok");
    } catch (e) {
      // 409 = nothing completed yet; say that plainly rather than "failed".
      toast(
        e.status === 409
          ? "Nothing to go back to — no step has been completed in this session yet."
          : `Couldn't go back: ${e.detail || e.message}`,
        "err"
      );
    }
  }

  async function doneNext(message) {
    const nk = session?.current_node_key;
    if (!nk) { await claimAndGuideNext(); return; }
    const output = composerText.value.trim();
    if (output) { await submitEvidence(nk, output); return; }
    const res = await api.post(`/assist/${sessionId}/track`, {
      message: message || "The operator says this step is done — assess and advance.",
      node_key: nk, history: historyForGuide(),
    });
    const v = res?.verdict || {};
    if (res?.action === "advanced") {
      toast(`✓ Step ${res.retired_prior_step || nk} closed out.`, "ok");
      await claimAndGuideNext();
      return;
    }
    if (res?.action === "added_step" && res.step?.node_key) {
      toast(`New step ${res.step.node_key} added — walking you through it.`, "ok");
      await load();
      guideCurrent();
      return;
    }
    if (res?.action === "finalized" || v.verdict === "finalize") {
      await load();
      appendBubble("assistant", "done", "🎉 That was the last step — the session is wrapping up.");
      return;
    }
    // §17.890 — a TYPED claim ("done", "it worked") must never dead-end here:
    // the operator has explicitly said the step is complete, and the server
    // now honors completion claims (operator-affirmed commit) even when the
    // verifier can't confirm. Submit the claim as the step's evidence and
    // advance — repeating "I already did that" is exactly the loop this kills.
    if (message) {
      await submitEvidence(nk, message, { quiet: true });
      return;
    }
    // Tracker not confident / thinks the step is still open — say so honestly
    // and name the ways forward. NEVER a bare "enter text" error.
    appendBubble("assistant", "track",
      (v.reason ? `${v.reason}\n\n` : "The tracker isn't sure this step is finished yet.\n\n") +
      "To close it out: paste what happened (output, a result, even one line) and press ✓ again, " +
      "type \"done\" to confirm it's complete on your word — or ⏩ Skip to move on without verification.");
  }

  // Verbs ordered as the loop runs (research: labeled contextual actions over
  // icon mysteries; primary path visually distinct from escape hatches).
  const verbsBar = el(
    "div",
    { class: "row row-wrap assist-verbs" },
    verb("✓ Done → next step", "Close out the current step (uses the box text as evidence when present, the conversation otherwise) and walk into the next one", () => doneNext()),
    // §17.901 — the undo for the button immediately to its left. "✓ Done" was
    // a one-way door: a mis-click closed a step that wasn't finished, and
    // "↻ Re-show step" re-presents whatever the pointer moved TO — the NEXT
    // step, which is why going back landed somewhere unrecognisable.
    verb("↩ Back a step", "Undo the last completed step and return to it — its walkthrough is kept exactly as it was", async () => {
      await stepBack();
    }),
    verb("↻ Re-show step", "Re-present the current step's walkthrough", async () => {
      // §17.868 — one server-side stream (claim-if-needed + premise + guide).
      await runTurnStream({ command: "guide" });
    }),
    verb("🔧 Fix error", "Paste the error in the box first — get a diagnosis for YOUR environment", async () => {
      const err = composerText.value.trim();
      if (!err) { toast("Paste the error into the box first.", "err"); return; }
      composerText.value = "";
      appendBubble("operator", "fix", err);
      const res = await api.post(`/assist/${sessionId}/fix`, {
        error: err, node_key: session?.current_node_key || null, history: historyForGuide(),
      });
      // §17.876 — same honest fallback as the server turn loop.
      appendBubble("assistant", "fix", res.fix || "I couldn't produce a fix this time — the model returned no usable answer after several attempts. Press the button again to retry, or paste just the last ~50 lines of the error output.");
      load();
    }),
    el("span", { class: "spacer" }),
    verb("⏩ Skip", "Skip the current step (recorded, revisitable)", async () => {
      const nk = session?.current_node_key;
      if (!nk) { toast("No step in flight.", "err"); return; }
      await api.post(`/assist/${sessionId}/submit`, { node_key: nk, action: "skip" });
      toast(`Step ${nk} skipped.`, "ok");
      load();
    }),
    verb("🤝 Engine does it", "Hand this step to the engine to do autonomously (LLM work only — never your machine)", async () => {
      const nk = session?.current_node_key;
      if (!nk) { toast("No step in flight.", "err"); return; }
      await api.post(`/assist/${sessionId}/handoff`, { node_key: nk, mode: "single" });
      toast(`Step ${nk} handed to the engine.`, "ok");
      load();
    }),
    verb("⏸", "Pause / resume the session", async () => {
      const paused = session?.status === "paused";
      await api.post(`/assist/${sessionId}/${paused ? "resume" : "pause"}`);
      toast(paused ? "Session resumed." : "Session paused.", "ok");
      load();
    })
  );
  const composer = el(
    "div",
    { class: "chat-composer" },
    // Top row: what the engine needs (left) · step verbs (right).
    el("div", { class: "composer-top" }, checklistPanel, el("span", { class: "spacer" }), verbsBar),
    // §17.933 — a pending re-plan is a compact chip docked with the input the
    // operator answers in, not a full card wedged above the step. Clicking it
    // reopens the modal that explains the changes.
    replanSlot,
    // Full-width input below.
    composerText,
    el("div", { class: "composer-actions" }, guideBtn, el("span", { class: "spacer" }), sendBtn)
  );

  const header = el(
    "div",
    { class: "view-header" },
    el("div", {}, el("h1", { text: "Assistant" }), el("div", { class: "sub mono", text: shortId(sessionId) })),
    el("div", { class: "header-actions" }, el("a", { class: "btn btn-sm btn-ghost", href: "#/assist", text: "← Sessions" }), el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => load() }))
  );

  const main = el("div", { class: "chat-main assist-main" }, transcript, composer);
  // §17.845 — the editable living brief rides with the session (mounted once
  // the session tells us its job).
  const briefSlot = el("div", { class: "assist-brief-slot" });
  mount(container, header, contractCard(), stepHero, main, briefSlot, belowGrid);
  let briefMounted = false;

  // 📍 Current-step hero — where am I, what's the loop position (§17.738/741
  // surface the recap in chat; this pins the essentials above it).
  // §17.938 — the step picker. Until now the only navigation was ✓ Done
  // (forward one), ↩ Back a step (back one, terminal steps only) and ↻ Re-show
  // (stay put); reaching any other step meant walking the whole plan. The
  // operator asked for "a simpler means to jump between the different nodes
  // within the chat". Terminal steps stay VISIBLE but disabled — seeing the
  // shape of the plan is half the value, and jumping to one would either
  // un-complete finished work or silently do nothing.
  const STEP_ICON = {
    committed: "✅", skipped: "⏩", handed_off: "🤝",
    presented: "📍", pending: "○", awaiting_input: "💬",
  };

  function renderStepPicker(currentKey) {
    if (!steps.length) return null;
    const sel = el("select", { class: "step-picker", title: "Jump to a step" });
    for (const st of steps) {
      const icon = STEP_ICON[st.step_status] || "○";
      const terminal = !["pending", "presented", "awaiting_input"].includes(st.step_status);
      const running = st.node_status === "running";
      const label = `${icon} ${st.node_key} — ${(st.title || "").slice(0, 58)}`
        + (running ? "  (engine is running this)" : "");
      const opt = el("option", { value: st.node_key, text: label });
      if (terminal || running) opt.disabled = true;
      if (st.node_key === currentKey) opt.selected = true;
      sel.append(opt);
    }
    sel.addEventListener("change", async () => {
      const target = sel.value;
      if (!target || target === currentKey) return;
      try {
        const res = await api.post(`/assist/${sessionId}/step/goto`, { node_key: target });
        await load();
        // The server records a durable turn for the move, so load() already
        // shows it. Render the PRESERVED walkthrough after it — deliberately
        // not guideCurrent(), for §17.901's reason: regenerating hands the
        // operator a different walkthrough for work they may be part-way
        // through.
        if (res?.guidance) appendBubble("assistant", "guide", res.guidance);
        else toast(`On ${res.node_key}. Press ✦ Guide me for a walkthrough.`, "ok");
      } catch (e) {
        toast(errText(e), "err");
        sel.value = currentKey || "";   // selection must not lie about state
      }
    });
    return sel;
  }

  function renderStepHero() {
    if (!session) return;
    const nk = session.current_node_key;
    const sc = session.step_counts || {};
    // §17.938 — `step_counts` is keyed by ASSIST-step status, where the
    // terminal state is `committed`; `done` is the dag_nodes vocabulary and
    // never appears here. Counting `sc.done` read 1/41 on a session with 26
    // committed steps — the progress badge had been understating the operator's
    // own progress by an order of magnitude. Count every terminal state, and
    // keep `done` in the sum so the badge stays right if the shape ever changes.
    const doneN = (sc.committed || 0) + (sc.done || 0)
      + (sc.skipped || 0) + (sc.handed_off || 0);
    const totalN = Object.values(sc).reduce((a, b) => a + b, 0);
    const cur = steps.find((x) => x.node_key === nk);
    stepHero.classList.remove("hidden");
    mount(
      stepHero,
      el("div", { class: "row row-wrap" },
        el("span", { class: "step-hero-pin", text: "📍" }),
        nk
          ? el("span", { class: "step-hero-title" }, el("strong", { text: `Step ${nk}` }),
              (cur?.title || session.current_node_title) ? ` — ${cur?.title || session.current_node_title}` : "")
          : el("span", { class: "step-hero-title dim", text: session.status === "completed" ? "Session complete 🎉" : "No step claimed — press Next step to begin" }),
        el("span", { class: "spacer" }),
        totalN ? el("span", { class: "tag", text: `${doneN}/${totalN} steps done` }) : null,
        statusBadge(session.status)),
      steps.length
        ? el("div", { class: "row row-wrap step-hero-nav" },
            el("span", { class: "dim small", text: "Jump to:" }),
            renderStepPicker(nk))
        : null,
      nk
        ? el("div", { class: "step-hero-loop dim", text: "The loop: ✦ Guide me → do it on your machine → paste what happened → ✓ Submit results" })
        : null
    );
  }

  composerText.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      sendMessage();
    }
  });

  // §17.816 — append one bubble immediately (verb actions render optimistic
  // feedback; load() then reconciles with the durable transcript).
  function appendBubble(role, kind, content) {
    transcript.append(bubble(role, kind, content, new Date().toISOString()));
    stick();
  }

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
    // §17.890 — never rebuild the DOM out from under an active selection.
    if (selectionWithin(transcript)) { transcriptRenderDeferred = true; return; }
    transcriptRenderDeferred = false;
    if (!turns.length) {
      mount(
        transcript,
        el(
          "div",
          { class: "empty-state small" },
          el("p", { text: "Nothing yet — start with the walkthrough of your first step." }),
          el("button", {
            class: "btn btn-primary",
            text: "✦ Guide me through the current step",
            onClick: async () => {
              if (!session?.current_node_key) await api.get(`/assist/${sessionId}/next`).catch(() => {});
              await load();
              guideCurrent();
            },
          })
        )
      );
      return;
    }
    mount(transcript, ...turns.map((t) => bubble(t.role, t.kind, t.content, t.created_at)));
    // §17.870 — the ephemeral tail: output the CURRENT turn rendered live that
    // is not (yet, or ever) in the durable transcript. The live incident: a
    // cached walkthrough replay streamed to the screen, then the end-of-turn
    // load() rebuilt the transcript from durable turns — which correctly
    // dedupe cached replays (§17.812) — and ERASED what the operator was
    // reading ("flashed an answer... but did nothing"). Entries drop out
    // automatically once an identical durable turn exists.
    // §17.871 — dedupe by TIME, not proximity: only a durable turn captured
    // DURING the current turn (i.e. this very turn's own server-side capture)
    // may replace an ephemeral entry. Both earlier heuristics re-created the
    // "pressed Guide, saw nothing" failure whenever identical content already
    // existed in the transcript — full-history dedupe always, newest-2 dedupe
    // exactly when the walkthrough had been shown just before the operator's
    // last message (the live home-lab T14 case: the newest-2 turns WERE the
    // old walkthrough + their paste).
    // §17.929 — the operator's own un-persisted messages, rendered BEFORE the
    // assistant tail (they said it first). An entry retires the moment an
    // operator turn with the same text exists durably, so the steady state is
    // an empty list and no bubble is ever shown twice.
    // Retire ONLY against turns that came back from the server. Comparing
    // against `turns` wholesale matched the optimistic copy pushed by
    // sendMessage() and cleared the entry on its very first render — which
    // left the guard doing nothing at all, the exact bug it exists to stop.
    pendingOps = pendingOps.filter((o) => !turns.some(
      (t) => t.role === "operator" && !t._pending
        && (t.content || "").trim() === o.content.trim()));
    for (const o of pendingOps) {
      transcript.append(bubble("operator", o.kind, o.content, o.created_at));
    }
    const cutoff = turnStartedAt || "9999";
    for (const e of ephemeralTail) {
      const dup = turns.some((t) =>
        (t.created_at || "") >= cutoff &&
        (t.content || "").trim() === e.content.trim());
      if (!dup && e.content.trim()) transcript.append(bubble("assistant", e.kind, e.content, e.at));
    }
    stick();
  }

  // Lazily-fetched sidebar extras (checklist §17.707, environment §17.703) —
  // cached per load cycle; failures degrade to absent cards, never errors.
  let checklist = null;
  let environment = null;
  // §17.938 — the plan's steps, for the step picker in the hero.
  let steps = [];

  // §17.707 — what the engine still needs from YOU, docked left of the input
  // so it's in view exactly where you answer.
  function renderChecklist() {
    const checkItems = (checklist?.items || []).slice(0, 8);
    const provided = checklist?.provided || {};
    if (!checkItems.length) {
      checklistPanel.classList.add("hidden");
      return;
    }
    checklistPanel.classList.remove("hidden");
    mount(
      checklistPanel,
      el("div", { class: "side-title", text: `The engine needs from you (${checklist?.open_count ?? checkItems.filter((i) => !i.done).length} open)` }),
      ...checkItems.map((it) => {
        const done = it.done === true;
        const value = provided[it.node_key];
        return el("div", { class: "side-check" },
          el("span", { class: done ? "check-dot done" : "check-dot", text: done ? "✓" : "○" }),
          el("span", { class: done ? "check-text dim" : "check-text" },
            `${it.title || it.node_key}`,
            done && value ? el("span", { class: "faint", text: ` — ${value}` }) : null));
      })
    );
  }

  // §17.850 — pinned-values editor: KEY = value substitutions guidance must
  // use verbatim (the durable answer to "replace the placeholders"). Add/
  // update applies immediately (server merges per-key); ✕ clears (empty value
  // = delete). Auto-learned values (§17.490) appear here too.
  function subsEditor() {
    const subs = environment?.substitutions || {};
    const putSub = async (k, v) => {
      try {
        await api.req(`/assist/${sessionId}/env`, { method: "PUT", body: { substitutions: { [k]: v } } });
        toast(v ? `Pinned ${k} — walkthroughs will use it verbatim.` : `Cleared ${k}.`, "ok");
        load();
      } catch (e) { toast(`Could not save: ${e.detail || e.message}`, "err"); }
    };
    const keyIn = el("input", { class: "input input-sm subs-key", placeholder: "PLACEHOLDER_NAME" });
    const valIn = el("input", { class: "input input-sm subs-val", placeholder: "actual value" });
    const add = () => {
      const k = keyIn.value.trim().replace(/[<>]/g, "");
      const v = valIn.value.trim();
      if (!k || !v) { toast("Both a name and a value are needed.", "err"); return; }
      keyIn.value = ""; valIn.value = "";
      putSub(k, v);
    };
    valIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
    return el(
      "div",
      { class: "subs-editor" },
      el("div", { class: "side-title", text: "Pinned values" }),
      el("p", { class: "dim subs-hint", text: "Walkthroughs use these verbatim wherever the matching <PLACEHOLDER> would appear." }),
      ...Object.entries(subs).map(([k, v]) =>
        el("div", { class: "bp-item" },
          el("span", { class: "bp-item-text mono", text: `${k} = ${v}` }),
          el("button", { class: "btn btn-ghost btn-sm bp-remove", text: "✕", title: "Clear this pin", onClick: () => putSub(k, "") }))),
      el("div", { class: "row bp-add-row" }, keyIn, valIn, el("button", { class: "btn btn-sm", text: "＋ Pin", onClick: add }))
    );
  }

  // Session / Steps / notes / facts / environment — the card row below the chat.
  function renderBelow() {
    if (!session) return;
    if (verbBtns["⏸"]) verbBtns["⏸"].textContent = session.status === "paused" ? "▶" : "⏸";
    const sc = session.step_counts || {};
    const notes = (session.notes || []).slice(-6).reverse();
    const facts = (session.memory_facts || []).slice(-8).reverse();
    const envLines = [
      ...(environment?.profile ? [environment.profile] : []),
      ...((environment?.facts || []).map((f) => (typeof f === "string" ? f : f.text || ""))),
    ].filter(Boolean).slice(0, 5);
    mount(
      belowGrid,
      el("div", { class: "card card-pad side-block" },
        el("div", { class: "side-title", text: "Session" }),
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
      // §17.703 — the machine the engine believes you're on; empty until taught.
      // §17.850 — plus the pinned-values editor (operator-set substitutions
      // walkthroughs must use verbatim — kills recurring <PLACEHOLDER>s).
      el("div", { class: "card card-pad side-block" },
        el("div", { class: "side-title", text: "Your environment (as tracked)" }),
        ...envLines.map((l) => el("div", { class: "side-fact", text: l })),
        subsEditor()
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
      const [s, t, cl, env, st] = await Promise.all([
        api.get(`/assist/${sessionId}`),
        api.get(`/assist/${sessionId}/turns`),
        api.get(`/assist/${sessionId}/checklist`).catch(() => null),
        api.get(`/assist/${sessionId}/env`).catch(() => null),
        api.get(`/assist/${sessionId}/steps`).catch(() => null),
      ]);
      if (disposed) return;
      session = s;
      turns = t.turns || [];
      // Transient checklist/env fetch failures keep the last known value —
      // a blip must not blank the needs-from-you panel mid-session.
      if (cl !== null) checklist = cl;
      if (env !== null) environment = env?.environment ?? null;
      if (st !== null) steps = st?.steps || [];
      header.querySelector(".sub").textContent = s.job_title || shortId(sessionId);
      if (!briefMounted && s.job_id) {
        briefMounted = true;
        mount(briefSlot, briefPanel(String(s.job_id)));
      }
      // §17.863 — a stashed-but-unresolved re-plan proposal re-renders after
      // any reload (the server now exposes it on the session read); resolving
      // it clears the slot.
      if (s.pending_replan) renderReplanProposal(s.pending_replan);
      else { lastReplanSig = null; mount(replanSlot); }
      renderStepHero();
      renderTranscript();
      renderChecklist();
      renderBelow();
    } catch (e) {
      if (!disposed) mount(transcript, errorPanel(e, () => load()));
    }
  }

  function historyForGuide() {
    return turns.slice(-8).map((t) => ({ role: t.role, content: t.content }));
  }

  // §17.848 — typed advance intents ("next step", "done", "it worked",
  // "continue") route through the progress tracker instead of only getting a
  // guidance monologue (the operator typed "next step" and the assistant just
  // talked). NARROW on purpose — short, unambiguous phrases only (§17.763's
  // lesson: fuzzy phrase gates go too eager); anything longer is a real
  // message and takes the normal guidance path.
  const ADVANCE_RE = /^(next( step)?|done|finished|complete(d)?|continue|move on|it worked|works|all set|step (is )?done)[.! ]*$/i;

  // §17.862/863 — readable error text. FastAPI error `detail` can be an
  // object or a 422 validation ARRAY; template-stringing those printed
  // "[object Object]" at the live operator.
  function errText(e) {
    const d = e?.detail;
    if (typeof d === "string" && d) return d;
    if (d != null) { try { return JSON.stringify(d).slice(0, 300); } catch { /* fall through */ } }
    return e?.message || String(e);
  }

  // §17.861/863 — the §17.677 note-triggered re-plan proposal.
  // §17.933 — REPRESENTED. It used to render as a permanent card pinned above
  // the step, listing raw `proposed_change` strings with no statement of what
  // the operator had said to cause it, what the plan currently assumes, or
  // what applying it would actually do. Sitting at the top of every reload, it
  // read as chrome and got scrolled past — the live session carried one
  // unresolved for hours. Now it ARRIVES as a modal at the moment it is
  // proposed, explains itself in full, and if dismissed without a decision
  // leaves only a compact chip beside the composer that reopens it. Nothing is
  // lost to navigation (the §17.863 invariant), but nothing squats at the top
  // either.
  let lastReplanSig = null;

  const REPLAN_ACTION_COPY = {
    revise: { icon: "✏️", label: "revise", blurb: "rewrite this step's instructions" },
    drop: { icon: "🗑️", label: "drop", blurb: "remove this step from the plan" },
    reopen: { icon: "↩️", label: "reopen", blurb: "put this finished step back in play" },
  };

  function replanChangeRow(ch) {
    const meta = REPLAN_ACTION_COPY[ch.action] || { icon: "•", label: ch.action || "change", blurb: "" };
    const change = ch.proposed_change || ch.summary || ch.change || ch.reason || "";
    return el("div", { class: "replan-change" },
      el("div", { class: "replan-change-head" },
        el("span", { class: "replan-act", text: `${meta.icon} ${meta.label}` }),
        el("span", { class: "mono replan-node", text: ch.node_key || "" }),
        meta.blurb ? el("span", { class: "dim small", text: `— ${meta.blurb}` }) : null),
      ch.current_assumption
        ? el("div", { class: "replan-line" },
            el("span", { class: "replan-tag dim", text: "Plan assumes now" }),
            el("span", { text: ch.current_assumption }))
        : null,
      change
        ? el("div", { class: "replan-line" },
            el("span", { class: "replan-tag", text: "Change to" }),
            el("span", { text: change }))
        : null);
  }

  // The outcome popup — brief, centred, with the REAL counts (§17.865).
  const ackPopup = (icon, title, body) => {
    const overlay = el("div", { class: "ack-overlay" },
      el("div", { class: "card ack-card" },
        el("span", { class: "ack-icon", text: icon }),
        el("strong", { text: title }),
        body ? el("p", { class: "dim", text: body }) : null));
    overlay.addEventListener("click", () => overlay.remove());
    document.body.append(overlay);
    setTimeout(() => overlay.remove(), 2600);
  };

  function renderReplanProposal(p, { open = false } = {}) {
    const changes = p.proposals || [];
    if (!changes.length) { mount(replanSlot); return; }
    const sig = JSON.stringify(changes.map((c) => [c.node_key, c.action, c.proposed_change]));
    const isNew = sig !== lastReplanSig;
    lastReplanSig = sig;

    const counts = changes.reduce((a, c) => { a[c.action] = (a[c.action] || 0) + 1; return a; }, {});
    const countText = ["revise", "drop", "reopen"]
      .filter((k) => counts[k])
      .map((k) => `${counts[k]} to ${k}`).join(" · ");

    let overlay = null;
    const closeModal = () => { if (overlay) { overlay.remove(); overlay = null; } };

    const resolve = async (decision) => {
      const n = changes.length;
      closeModal();
      mount(replanSlot, el("div", { class: "replan-chip busy" },
        el("span", { class: "spin" }),
        el("span", { text: decision === "apply"
          ? ` Applying ${n} change${n === 1 ? "" : "s"}…`
          : " Discarding…" })));
      try {
        const res = await api.post(`/assist/${sessionId}/replan/apply`, { decision });
        mount(replanSlot);
        if (decision === "apply") {
          const rev = (res.revised || []).length;
          const drop = (res.dropped || []).length;
          ackPopup("✅", "Plan updated",
            `${rev} step(s) revised, ${drop} dropped. Continuing on the revised plan…`);
          toast(`Plan updated — ${rev} revised, ${drop} dropped.`, "ok");
          await load();
          await claimAndGuideNext();
          return;
        }
        ackPopup("👍", "Proposal discarded", "The plan is unchanged. I won't re-suggest this one.");
        toast("Proposal discarded.", "ok");
      } catch (e) {
        // Restore the chip so the operator can retry; the proposal is still
        // staged server-side when apply failed.
        toast(errText(e), "err");
        lastReplanSig = null;
        renderReplanProposal(p);
        return;
      }
      load();
    };

    const openModal = () => {
      closeModal();
      const applyBtn = el("button", { class: "btn btn-primary", text: `Apply ${changes.length === 1 ? "this change" : "these changes"}` });
      const keepBtn = el("button", { class: "btn btn-ghost", text: "Keep plan as-is" });
      const laterBtn = el("button", { class: "btn btn-ghost btn-sm", text: "Decide later" });
      applyBtn.addEventListener("click", () => resolve("apply"));
      keepBtn.addEventListener("click", () => resolve("discard"));
      laterBtn.addEventListener("click", () => { closeModal(); renderChip(); });

      overlay = el("div", { class: "modal-overlay" },
        el("div", { class: "card modal-card replan-modal" },
          el("div", { class: "modal-head" },
            el("strong", { text: "📋 This changes the plan" }),
            el("button", { class: "btn btn-ghost btn-sm", text: "✕",
              onClick: () => { closeModal(); renderChip(); } })),
          // WHY — the operator's own words are the cause; show them.
          p.note_text
            ? el("div", { class: "replan-why" },
                el("div", { class: "dim small", text: "Because you told me:" }),
                el("blockquote", { class: "replan-quote", text: p.note_text }))
            : null,
          el("p", { class: "dim small", text:
            (changes.length === 1
              ? "That conflicts with what a step in your plan currently assumes. Here is the change I suggest"
              : "That conflicts with what some steps in your plan currently assume. Here are the changes I suggest") +
            " — nothing is applied until you choose." }),
          el("div", { class: "replan-changes" }, ...changes.map(replanChangeRow)),
          el("p", { class: "dim small", text: countText
            ? `If you apply: ${countText}. Your finished work is kept; only the steps listed above change.`
            : "Nothing is applied until you choose." }),
          el("div", { class: "modal-actions" }, applyBtn, keepBtn,
            el("span", { class: "spacer" }), laterBtn)));
      overlay.addEventListener("click", (ev) => {
        if (ev.target === overlay) { closeModal(); renderChip(); }
      });
      document.body.append(overlay);
    };

    const renderChip = () => {
      const chip = el("div", { class: "replan-chip" },
        el("span", { text: `📋 ${changes.length} proposed plan change${changes.length === 1 ? "" : "s"}` }),
        el("button", { class: "btn btn-sm btn-primary", text: "Review", onClick: openModal }));
      mount(replanSlot, chip);
    };

    renderChip();
    // Pop it the moment it is PRESENTED — a freshly proposed change, or one
    // the operator has not seen in this view yet.
    if (open || isNew) openModal();
  }




  // §17.868 — ONE stream consumer for the server-side turn loop. The night of
  // §17.861–867 proved client-side sequencing (capture → decide → dispatch →
  // claim → guide as separate calls with shared abort state) fails at every
  // seam: an impatient click killed invisible in-flight chains and rendered
  // nothing. The server now owns the loop (POST /assist/{sid}/message) and
  // streams a status frame at EVERY stage; this function only renders frames.
  // A second trigger while a turn is active is an explicit Stop, with the
  // button visibly armed — never a silent kill.
  // §17.869 — the turn runs DETACHED server-side (assist_turn_runs); this
  // consumer merely tails frames. `resumeRunId` re-attaches after a reload:
  // the tail replays every missed frame, then follows live. Stopping the tail
  // (■ Stop) stops WATCHING — the turn itself always completes server-side
  // and lands in the transcript.
  async function runTurnStream(body, resumeRunId) {
    if (guiding) {
      toast("Still working on the last turn — press ■ Stop first, or wait.", "");
      return;
    }
    guiding = true;
    guideBtn.textContent = "■ Stop";
    sendBtn.disabled = true;
    abort = new AbortController();
    ephemeralTail = [];  // §17.870 — fresh turn, fresh tail
    // §17.871 — small slack for client/server clock skew; a capture stamped
    // slightly "before" our local start must still count as ours.
    turnStartedAt = new Date(Date.now() - 15000).toISOString();
    let statusEl = null;
    const setStatusLine = (t) => {
      if (!statusEl) {
        statusEl = el("div", { class: "msg sys" }, el("div", { class: "msg-body dim" }));
        transcript.append(statusEl);
      }
      statusEl.firstChild.textContent = t;
      stick();
    };
    const clearStatusLine = () => { if (statusEl) { statusEl.remove(); statusEl = null; } };
    let live = null, liveBody = null, acc = "";
    const ensureLive = () => {
      if (live) return;
      liveBody = el("div", { class: "msg-body md" }, el("span", { class: "spin" }));
      live = el("div", { class: "msg as streaming" },
        el("div", { class: "msg-meta" },
          el("span", { class: "msg-role", text: "assistant" }),
          el("span", { class: "msg-kind", text: "guiding" })),
        liveBody);
      transcript.append(live);
      stick();
    };
    const streamArgs = resumeRunId
      ? [`/assist/${sessionId}/message/${resumeRunId}/tail`, { method: "GET", signal: abort.signal }]
      : [`/assist/${sessionId}/message`, {
          body: { history: historyForGuide(), node_key: session?.current_node_key || null, ...body },
          signal: abort.signal,
        }];
    try {
      for await (const { event, data } of api.stream(...streamArgs)) {
        if (disposed) break;
        switch (event) {
          case "assist_turn_started":
            break;
          case "assist_turn_status":
            setStatusLine(data?.text || "…");
            break;
          case "assist_note_recorded": {
            clearStatusLine();
            const noteMsg = `📝 Noted (${data?.kind || "note"}).` +
              (data?.retracted ? ` Retracted ${data.retracted} stale fact(s).` : "");
            appendBubble("assistant", "note", noteMsg);
            ephemeralTail.push({ kind: "note", content: noteMsg, at: new Date().toISOString() });
            break;
          }
          case "assist_replan_proposal":
            // §17.933 — a proposal arriving mid-turn is being PRESENTED:
            // open the explanation now rather than parking a card.
            if (data?.proposal) renderReplanProposal(data.proposal, { open: true });
            break;
          case "assist_answer":
            clearStatusLine();
            appendBubble("assistant", data?.kind || "ask", data?.text || "");
            ephemeralTail.push({ kind: data?.kind || "ask", content: data?.text || "", at: new Date().toISOString() });
            break;
          case "assist_step_outcome":
            toast(`Step ${data?.node_key || ""}: ${data?.status || "recorded"}.`,
              data?.status === "committed" ? "ok" : "");
            break;
          case "assist_guide_delta":
            clearStatusLine();
            ensureLive();
            acc += (data && data.text) || "";
            // §17.890 — repainting the live bubble kills a selection held in
            // it; acc is cumulative, so the next unselected delta catches up.
            if (!selectionWithin(transcript)) liveBody.innerHTML = mdToHtml(acc);
            stick();
            break;
          case "assist_guide_done":
            if (live) live.classList.remove("streaming");
            if (acc.trim()) ephemeralTail.push({ kind: "guide", content: acc, at: new Date().toISOString() });
            break;
          case "assist_turn_done":
            break;
          case "error":
            clearStatusLine();
            appendBubble("assistant", "note", `⚠ ${data?.detail || "turn error"}`);
            break;
          default:
            break;
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") toast(errText(e), "err");
    } finally {
      guiding = false;
      abort = null;
      sendBtn.disabled = false;
      guideBtn.textContent = "✦ Guide me";
      clearStatusLine();
      if (live) live.classList.remove("streaming");
      load();
    }
  }

  // §17.869 — after any reload, re-attach to a still-running turn. The tail
  // replays what was missed; the operator never loses an in-flight turn to
  // navigation again.
  let resumeChecked = false;
  async function maybeResumeActiveTurn() {
    if (guiding || resumeChecked) return;
    resumeChecked = true;
    try {
      const act = await api.get(`/assist/${sessionId}/message/active`);
      if (act?.run_id && !guiding) {
        toast("Re-attaching to the turn that was still running…", "");
        await runTurnStream(null, act.run_id);
      }
    } catch { /* older server or none active — nothing to resume */ }
  }

  async function sendMessage() {
    const text = composerText.value.trim();
    if (!text || guiding) return;
    composerText.value = "";
    // §17.929 — record it in BOTH places: `turns` for the immediate paint, and
    // `pendingOps` so the reconcile at the end of the turn cannot erase it.
    // `_pending` marks this as the OPTIMISTIC copy: it lives in `turns` so it
    // paints at once and rides in historyForGuide(), but the retire-filter
    // below must not mistake it for the durable turn it is waiting for.
    const sent = {
      role: "operator", kind: "message", content: text,
      created_at: new Date().toISOString(), _pending: true,
    };
    turns.push(sent);
    pendingOps.push(sent);
    if (pendingOps.length > 50) pendingOps = pendingOps.slice(-50);
    renderTranscript();
    // Advance verbs stay a local fast-path (deterministic, closes the step
    // through the verified submit/track flow). EVERYTHING else is one server
    // turn — capture included (the server ingests the turn; no separate
    // /turn call to race it).
    if (ADVANCE_RE.test(text) && session?.current_node_key) {
      await doneNext(text);
      return;
    }
    await runTurnStream({ message: text, command: "message" });
  }

  async function guideCurrent() {
    if (guiding) {
      if (abort) abort.abort();  // the button reads "■ Stop" — explicit stop
      return;
    }
    await runTurnStream({ command: "guide" });
  }

  load().then(maybeResumeActiveTurn);

  return () => {
    disposed = true;
    document.removeEventListener("selectionchange", onSelChange);  // §17.890
    if (abort) abort.abort();
  };
}

export default function assist(container, params) {
  if (params && params.sessionId) return renderChat(container, params.sessionId);
  return renderPicker(container);
}
