// Assistant chat. Renders the assist session as a chat: transcript from
// /assist/{id}/turns (operator vs assistant bubbles, markdown), a context
// sidebar (step counts, current node, notes, memory facts), and a live
// step-guidance driver via /assist/{id}/guide/stream (SSE: assist_guide_delta
// / assist_guide_done). Message composer persists via /assist/{id}/turn.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, fmtDate, mdToHtml } from "../util.js";
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

  const transcript = el("div", { class: "chat-transcript" }, loading("Loading conversation…"));
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
  const replanSlot = el("div", {});
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
  async function submitEvidence(nk, output) {
    composerText.value = "";
    appendBubble("operator", "submit", output);
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
      appendBubble("assistant", "decision", res.message || "This step needs your input — see the question above and answer in the box.");
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
    // Tracker not confident / thinks the step is still open — say so honestly
    // and name the ways forward. NEVER a bare "enter text" error.
    appendBubble("assistant", "track",
      (v.reason ? `${v.reason}\n\n` : "The tracker isn't sure this step is finished yet.\n\n") +
      "To close it out: paste what happened (output, a result, even one line) and press ✓ again — " +
      "or ⏩ Skip to move on without verification.");
  }

  // Verbs ordered as the loop runs (research: labeled contextual actions over
  // icon mysteries; primary path visually distinct from escape hatches).
  const verbsBar = el(
    "div",
    { class: "row row-wrap assist-verbs" },
    verb("✓ Done → next step", "Close out the current step (uses the box text as evidence when present, the conversation otherwise) and walk into the next one", () => doneNext()),
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
      appendBubble("assistant", "fix", res.fix || "(no fix returned)");
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
  mount(container, header, contractCard(), replanSlot, stepHero, main, briefSlot, belowGrid);
  let briefMounted = false;

  // 📍 Current-step hero — where am I, what's the loop position (§17.738/741
  // surface the recap in chat; this pins the essentials above it).
  function renderStepHero() {
    if (!session) return;
    const nk = session.current_node_key;
    const sc = session.step_counts || {};
    const doneN = (sc.done || 0) + (sc.skipped || 0);
    const totalN = Object.values(sc).reduce((a, b) => a + b, 0);
    stepHero.classList.remove("hidden");
    mount(
      stepHero,
      el("div", { class: "row row-wrap" },
        el("span", { class: "step-hero-pin", text: "📍" }),
        nk
          ? el("span", { class: "step-hero-title" }, el("strong", { text: `Step ${nk}` }), session.current_node_title ? ` — ${session.current_node_title}` : "")
          : el("span", { class: "step-hero-title dim", text: session.status === "completed" ? "Session complete 🎉" : "No step claimed — press Next step to begin" }),
        el("span", { class: "spacer" }),
        totalN ? el("span", { class: "tag", text: `${doneN}/${totalN} steps done` }) : null,
        statusBadge(session.status)),
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
    transcript.scrollTop = transcript.scrollHeight;
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
    transcript.scrollTop = transcript.scrollHeight;
  }

  // Lazily-fetched sidebar extras (checklist §17.707, environment §17.703) —
  // cached per load cycle; failures degrade to absent cards, never errors.
  let checklist = null;
  let environment = null;

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
      const [s, t, cl, env] = await Promise.all([
        api.get(`/assist/${sessionId}`),
        api.get(`/assist/${sessionId}/turns`),
        api.get(`/assist/${sessionId}/checklist`).catch(() => null),
        api.get(`/assist/${sessionId}/env`).catch(() => null),
      ]);
      if (disposed) return;
      session = s;
      turns = t.turns || [];
      // Transient checklist/env fetch failures keep the last known value —
      // a blip must not blank the needs-from-you panel mid-session.
      if (cl !== null) checklist = cl;
      if (env !== null) environment = env?.environment ?? null;
      header.querySelector(".sub").textContent = s.job_title || shortId(sessionId);
      if (!briefMounted && s.job_id) {
        briefMounted = true;
        mount(briefSlot, briefPanel(String(s.job_id)));
      }
      // §17.863 — a stashed-but-unresolved re-plan proposal re-renders after
      // any reload (the server now exposes it on the session read); resolving
      // it clears the slot.
      if (s.pending_replan) renderReplanProposal(s.pending_replan);
      else mount(replanSlot);
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

  // §17.861 — render the §17.677 note-triggered re-plan proposal with
  // apply/discard controls. §17.863 — into the dedicated slot (idempotent:
  // load() re-renders the session's PENDING proposal after any reload, so a
  // surfaced proposal can no longer be lost to navigation).
  function renderReplanProposal(p) {
    const items = (p.proposals || []).map((ch) =>
      el("li", {},
        el("span", { class: "mono", text: ch.node_key || "" }),
        ch.action ? el("span", { class: "tag", text: ch.action }) : null,
        el("span", { text: ` ${ch.proposed_change || ch.summary || ch.change || ch.reason || JSON.stringify(ch).slice(0, 200)}` })));
    const applyBtn = el("button", { class: "btn btn-sm btn-primary", text: "Apply re-plan" });
    const discardBtn = el("button", { class: "btn btn-sm btn-ghost", text: "Keep plan as-is" });
    const card = el("div", { class: "card card-pad replan-card" },
      el("strong", { text: "📋 What you've told me affects the plan — proposed changes:" }),
      el("ul", { class: "brief-list" }, ...items),
      el("div", { class: "row" }, applyBtn, discardBtn));
    // §17.865 — the click must be unmistakable (live operator: applied a
    // 13-change proposal and saw "nothing"). The card flips to a live
    // progress state the moment a button is pressed, the outcome lands as a
    // brief centered popup with the REAL counts, and an apply flows straight
    // into the revised plan (load + claim-and-guide) — acknowledgment AND
    // movement, not a corner toast.
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
    const resolve = async (decision) => {
      const n = (p.proposals || []).length;
      mount(replanSlot, el("div", { class: "card card-pad replan-card" },
        el("div", { class: "row" },
          el("span", { class: "spin" }),
          el("strong", { text: decision === "apply"
            ? ` Applying ${n} change(s) to the plan…`
            : " Discarding the proposal…" }))));
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
        // Restore the card so the operator can retry; the proposal is still
        // staged server-side when apply failed.
        toast(errText(e), "err");
        renderReplanProposal(p);
        return;
      }
      load();
    };
    applyBtn.addEventListener("click", () => resolve("apply"));
    discardBtn.addEventListener("click", () => resolve("discard"));
    mount(replanSlot, card);
    replanSlot.scrollIntoView({ block: "nearest" });
  }




  // §17.868 — ONE stream consumer for the server-side turn loop. The night of
  // §17.861–867 proved client-side sequencing (capture → decide → dispatch →
  // claim → guide as separate calls with shared abort state) fails at every
  // seam: an impatient click killed invisible in-flight chains and rendered
  // nothing. The server now owns the loop (POST /assist/{sid}/message) and
  // streams a status frame at EVERY stage; this function only renders frames.
  // A second trigger while a turn is active is an explicit Stop, with the
  // button visibly armed — never a silent kill.
  async function runTurnStream(body) {
    if (guiding) {
      toast("Still working on the last turn — press ■ Stop first, or wait.", "");
      return;
    }
    guiding = true;
    guideBtn.textContent = "■ Stop";
    sendBtn.disabled = true;
    abort = new AbortController();
    let statusEl = null;
    const setStatusLine = (t) => {
      if (!statusEl) {
        statusEl = el("div", { class: "msg sys" }, el("div", { class: "msg-body dim" }));
        transcript.append(statusEl);
      }
      statusEl.firstChild.textContent = t;
      transcript.scrollTop = transcript.scrollHeight;
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
      transcript.scrollTop = transcript.scrollHeight;
    };
    try {
      for await (const { event, data } of api.stream(`/assist/${sessionId}/message`, {
        body: { history: historyForGuide(), node_key: session?.current_node_key || null, ...body },
        signal: abort.signal,
      })) {
        if (disposed) break;
        switch (event) {
          case "assist_turn_status":
            setStatusLine(data?.text || "…");
            break;
          case "assist_note_recorded":
            clearStatusLine();
            appendBubble("assistant", "note",
              `📝 Noted (${data?.kind || "note"}).` +
              (data?.retracted ? ` Retracted ${data.retracted} stale fact(s).` : ""));
            break;
          case "assist_replan_proposal":
            if (data?.proposal) renderReplanProposal(data.proposal);
            break;
          case "assist_answer":
            clearStatusLine();
            appendBubble("assistant", data?.kind || "ask", data?.text || "");
            break;
          case "assist_step_outcome":
            toast(`Step ${data?.node_key || ""}: ${data?.status || "recorded"}.`,
              data?.status === "committed" ? "ok" : "");
            break;
          case "assist_guide_delta":
            clearStatusLine();
            ensureLive();
            acc += (data && data.text) || "";
            liveBody.innerHTML = mdToHtml(acc);
            transcript.scrollTop = transcript.scrollHeight;
            break;
          case "assist_guide_done":
            if (live) live.classList.remove("streaming");
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

  async function sendMessage() {
    const text = composerText.value.trim();
    if (!text || guiding) return;
    composerText.value = "";
    turns.push({ role: "operator", kind: "message", content: text, created_at: new Date().toISOString() });
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
