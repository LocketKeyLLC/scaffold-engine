// Assistant chat. Renders the assist session as a chat: transcript from
// /assist/{id}/turns (operator vs assistant bubbles, markdown), a context
// sidebar (step counts, current node, notes, memory facts), and a live
// step-guidance driver via /assist/{id}/guide/stream (SSE: assist_guide_delta
// / assist_guide_done). Message composer persists via /assist/{id}/turn.
import * as api from "../api.js";
import { el, mount, shortId, timeAgo, fmtDate, mdToHtml, stickyScroll, selectionWithin } from "../util.js";
import { statusBadge, loading, errorPanel, toast, emptyState, openDialog } from "../components.js";
import { briefPanel } from "./brief_panel.js";

import { storage } from "../storage.js";

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
// Split markdown on TOP-LEVEL `## ` headings only (`###` stays inside its
// parent section, so a phased runbook's sub-steps don't each become a row).
// Sections that stay OPEN in a folded walkthrough: the one action to take
// (§17.741) and the finish line that says how to advance (§17.932).
export const GUIDE_OPEN_SECTION = /(?:\u{1F449}|\u2705|do this next|done when)/iu;

// §17.1013 — a FIX turn is not shaped like a walkthrough, and folding it with
// the walkthrough's rule hid the thing the operator needed.
//
// A guide leads with `## 👉 Do this next` carrying the whole immediate action,
// so folding `## Run this` underneath it loses nothing. A fix leads with a
// one-line summary and puts the actual steps in `## Fix` — so the same rule
// collapsed the only actionable section behind a row reading "Fix (6)". Live
// report: "without ANY walk through, though it is hidden under Fix." Measured
// on the turn they were looking at: 390 of 2,062 chars visible, with the six
// numbered clicks folded away.
export const FIX_OPEN_SECTION =
  /(?:\u{1F449}|\u2705|do this next|done when|^fix$|^steps?$|^what to do)/iu;

// The open-section rule for a bubble kind. Anything not explicitly shaped like
// a fix keeps the walkthrough rule.
// §17.1082 — what the transcript paints for operator messages, as data so a
// node test can pin it: durable turns once, and each un-persisted operator
// message ONCE more only while no durable copy of it exists. The optimistic
// copy sendMessage() pushes into `turns` is `_pending` and is NOT painted
// from `turns` — painting it there and again from pendingOps showed every
// operator message twice for the whole turn (live, 2026-09-15).
export function transcriptPlan(turns, pendingOps) {
  const durable = (turns || []).filter((t) => !t._pending);
  const pending = (pendingOps || []).filter((o) => !durable.some(
    (t) => t.role === "operator" && (t.content || "").trim() === (o.content || "").trim()));
  return { durable, pending };
}

// §17.1093 — the one decision behind "the proposal keeps re-appearing": a
// staged plan-change is SHOWN unless the operator already applied/discarded it,
// and it force-OPENS the modal only for a fresh in-turn proposal — never from a
// background poll or a reload, and never one closed with "Later". Pure so a
// node test can pin it.
// §17.1097 — announce-once. A NEW plan-change proposal auto-opens the modal
// exactly the first time it is seen (from ANY source — an in-turn stream or a
// fact-triggered background poll), then collapses to the "Review" chip and
// never re-opens. This replaces the §17.1093 `background` guard (which made
// the surfacing INCONSISTENT: in-turn proposals popped, background ones only
// ever showed a chip) with one rule that is both consistent AND cannot nag:
// the caller records the sig in `announced` on first render, so every later
// render (the 25 s poll included) sees firstSighting=false → chip only.
// `open`/`background` are accepted for call-site compatibility and ignored.
export function replanRenderDecision(sig, { announced, resolved, snoozed, open, background, isNew } = {}) {
  const inSet = (s) => !!(s && s.has && s.has(sig));
  if (inSet(resolved)) return { show: false, openModal: false, firstSighting: false };
  const firstSighting = !inSet(announced);
  const openModal = firstSighting && !inSet(snoozed);
  return { show: true, openModal, firstSighting };
}

export function openSectionFor(kind) {
  return kind === "fix" ? FIX_OPEN_SECTION : GUIDE_OPEN_SECTION;
}

export function splitGuideSections(md) {
  const lines = String(md || "").split("\n");
  const lead = [];
  const sections = [];
  let cur = null;
  let inFence = false;
  for (const line of lines) {
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence;
    const m = !inFence && /^##\s+(?!#)(.+?)\s*$/.exec(line);
    if (m) {
      cur = { title: m[1].trim(), body: [] };
      sections.push(cur);
      continue;
    }
    (cur ? cur.body : lead).push(line);
  }
  return { lead: lead.join("\n"), sections };
}

// A short count for the row label ("Prerequisites (3)") so the operator can
// judge whether it is worth opening without opening it.
export function sectionCount(body) {
  const items = (body.match(/^\s*(?:[-*]|\d+\.)\s+\S/gm) || []).length;
  return items > 1 ? ` (${items})` : "";
}

// §17.1011 — how many trailing turns stay expanded; older ones fold into
// one backlog row. 6 keeps the current step's guide + the operator's paste
// + the reply visible without re-mounting the whole session.
const ASSIST_RECENT_TURNS = 6;
const ASSIST_ONBOARD_KEY = "scaffold_assist_onboarded";
function contractCard(onDismiss, session, force) {
  if (!force && storage.get(ASSIST_ONBOARD_KEY)) return null;
  // §17.1011 — retire it once the operator has DONE the loop. Measured on the
  // live homelab session: this four-step "how assist mode works" card was
  // still on screen at step 37 of 41, directly above a step header that
  // re-explains the same loop ("✦ Guide me → do it on your machine → paste
  // what happened → ✓ Submit results"). Dismissal was per-browser localStorage
  // only, so any new browser, profile or cleared storage re-taught the loop to
  // someone two-thirds through a 41-step build. Committed steps are proof they
  // know it; the ? in the step header brings it back on demand.
  const sc = (session && session.step_counts) || {};
  if (!force && ((sc.committed || 0) + (sc.done || 0)) > 0) return null;
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
      el("button", { class: "btn btn-ghost btn-sm", text: "Got it", onClick: (e) => { storage.set(ASSIST_ONBOARD_KEY, "1"); e.target.closest(".assist-contract")?.remove(); onDismiss?.(); } })),
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
// §17.1054 — the newest durable turn id (0 when none / optimistic-only).
// §17.1095 — what to do when a turn was killed by an engine restart (the
// "died" frame). If the operator's last message already got an answer, the
// restart is stale noise — reassure, don't alarm. If it was NOT answered,
// hand its text back so "resend" is one click, not a retype of a 48-line
// paste. Pure so a node test can pin it.
export function restartRecovery(turns, detail) {
  const isRestart = /restarted mid-turn|stalled for more than/i.test(detail || "");
  if (!isRestart) return { kind: "generic", text: "" };
  const ops = (turns || []).filter((t) => t.role === "operator" && !t._pending && (t.content || "").trim());
  if (!ops.length) return { kind: "generic", text: "" };
  const last = ops[ops.length - 1];
  const lastId = typeof last.id === "number" ? last.id : -1;
  const answered = (turns || []).some((t) => t.role === "assistant" && typeof t.id === "number" && t.id > lastId);
  return answered ? { kind: "answered", text: "" } : { kind: "restore", text: last.content };
}

// §17.1096 — the in-product HELP source. The controls exist and the engine
// does a lot on its own (restart recovery, state verification, plan-change
// proposals), but nothing in the UI told the operator what any of it means:
// the button titles were hover-only (invisible on touch, undiscoverable) and
// the behaviours were explained nowhere. This is ONE source of truth — the
// "?" panel is built from it, and a ci-tier-0 gate (test_assist_help_wiring)
// fails if a `verb(...)` in this file has no entry here. A control cannot
// ship without its plain-language explanation.
// §17.1117 (ledger U-8) — the liveness dot's state from the time since the
// engine last said anything. Exported for the node tests; the thresholds are
// the ones the help panel quotes.
export const PULSE_QUIET_MS = 45000;
export const PULSE_STALE_MS = 120000;
export function pulseState(quietMs) {
  const q = Math.max(0, quietMs | 0);
  return q >= PULSE_STALE_MS ? "stale" : q >= PULSE_QUIET_MS ? "quiet" : "live";
}

export const ASSIST_HELP = {
  // keyed by the exact label passed to verb(): what it does, in the
  // operator's words, and when they'd reach for it.
  controls: {
    "✓ Done → next step": "Marks the step you're on as finished and walks you into the next one. If you pasted output into the box, that becomes the evidence the step was done; otherwise the conversation is used.",
    "↩ Back a step": "Undoes the last step you completed and returns you to it — its walkthrough comes back exactly as it was. Use this if you pressed ✓ too soon.",
    "↻ Re-show step": "Shows the current step's walkthrough again, from the top. Nothing changes — it just re-presents what to do.",
    "🩺 Verify state": "Checks what is actually running on your machines against what the plan believes, using one read-only script. Where they disagree, it walks you through the repairs one at a time. Reach for this when a step keeps failing or you're not sure the plan is still accurate.",
    "🔧 Fix error": "Paste the error into the box first, then press this. You get a diagnosis for YOUR environment — the engine reads it against what it already knows about your machines, not a generic answer.",
    "⏩ Skip": "Skips the current step for now. It's recorded and you can come back to it later.",
    "🤝 Engine does it": "Hands the step to the engine to finish on its own — but only work the engine itself can do (thinking, writing, planning). It never touches your machine for you.",
    "↶ Restore a reopened step": "If a re-plan or a state check reopened a step you'd already finished, this puts it back to done with the evidence it had. It's refused once you've done more work on that step, so it can't erase real progress.",
    "⏸": "Pauses the session so nothing runs while you step away; press it again to resume right where you left off.",
  },
  // things the engine does on its own that the operator should be able to
  // understand — the behaviours built this session that had no UI explanation.
  behaviors: [
    { title: "If the engine restarts mid-step", plain: "Nothing is lost. Every finished step and every message is saved. If a restart interrupts a message you'd sent, the engine puts your text back in the box and asks you to press Send to pick up exactly where you were." },
    { title: "The status line above the box", plain: "A spinner means the engine is working on your step right now, with a clock counting how long. The small dot beside it blinks each time the engine reports progress; it turns grey after 45 seconds of silence and the line then tells you what to do, and red after two minutes — if it stays red, press ■ Stop and resend your message." },
    { title: "When the plan and reality disagree", plain: "The engine keeps a picture of your machines from what you've pasted. If a step assumes something that isn't true anymore, it says so and offers to fix the plan rather than pushing you through a step that can't work. 🩺 Verify state is how you ask it to check on purpose." },
    { title: "A plan-change proposal", plain: "When something you've told the engine changes what the plan should be, a card opens once showing the change as before → after — what the step says now and what it would say instead. Apply it, Keep the plan as-is, or Decide later. After that it collapses to a small 'Review' chip by the box and won't pop up again." },
    { title: "Why it won't invent values", plain: "The engine refuses to put an IP address, port, version or URL into an instruction unless it came from something you actually showed it. If it doesn't know a value yet, it asks you to run a command that prints it — that's deliberate, so it never sends you to the wrong place." },
    { title: "What the engine knows about your setup", plain: "As you paste command output, the engine builds a map of your machines — names, addresses, and how traffic reaches them — and shows it in Session details below. That map is what keeps every step pointed at the right machine." },
  ],
};

// Pure: the help panel's sections, ready for the renderer. Kept out of the
// DOM so it can be node-tested and so the wiring gate can read it.
export function helpSections() {
  return [
    { heading: "The buttons", kind: "controls",
      items: Object.entries(ASSIST_HELP.controls).map(([term, plain]) => ({ term, plain })) },
    { heading: "What the assistant does on its own", kind: "behaviors",
      items: ASSIST_HELP.behaviors.map((b) => ({ term: b.title, plain: b.plain })) },
  ];
}

// The set of control labels the help source covers — the gate compares this
// against the verb(...) calls in the file.
export function helpControlLabels() {
  return Object.keys(ASSIST_HELP.controls);
}

export function maxTurnId(turns) {
  let m = 0;
  for (const t of turns || []) {
    const id = Number(t && t.id);
    if (Number.isFinite(id) && id > m) m = id;
  }
  return m;
}

// §17.1054 — an ephemeral (streamed this turn) assistant entry is already in
// the durable transcript when a turn captured AFTER the turn-start watermark
// carries the same text. Clock-free: ids only ever grow, and the server
// assigns them, so a browser clock that runs fast or slow cannot break it.
export function ephemeralIsDurable(turns, entry, maxIdAtTurnStart) {
  const want = ((entry && entry.content) || "").trim();
  if (!want) return true;
  return (turns || []).some((t) => {
    const id = Number(t && t.id);
    return Number.isFinite(id) && id > (maxIdAtTurnStart || 0)
      && ((t.content || "").trim() === want);
  });
}

// §17.1055 — where the operator is in the plan, from the ordered /steps list:
// 1-based position of the current step, the nearest finished step before it
// and the nearest unfinished step after it. Pure, so the strip can be tested
// without a DOM. `total` is the list length (the plan as ordered), not the
// step_counts sum — the two agree except mid-mutation.
const TERMINAL_STEP = new Set(["committed", "done", "skipped", "handed_off"]);
export function planPosition(steps, currentKey) {
  const list = Array.isArray(steps) ? steps : [];
  const idx = list.findIndex((x) => x && x.node_key === currentKey);
  let prev = null, next = null;
  if (idx >= 0) {
    for (let i = idx - 1; i >= 0; i--) {
      if (TERMINAL_STEP.has(list[i].step_status)) { prev = list[i]; break; }
    }
    for (let i = idx + 1; i < list.length; i++) {
      if (!TERMINAL_STEP.has(list[i].step_status)) { next = list[i]; break; }
    }
  }
  const done = list.filter((x) => TERMINAL_STEP.has(x && x.step_status)).length;
  return { index: idx >= 0 ? idx + 1 : 0, total: list.length, done, prev, next };
}

const SIDEBAR_COLLAPSED_KEY = "scaffold_sidebar_collapsed";
const ASSIST_MORE_KEY = "scaffold_assist_more_open";

export function renderChat(container, sessionId, opts = {}) {
  // §17.1055 — inside the job hub the hub header already names the job and
  // owns the tabs, so the view's own "Assistant / <id> / ← Sessions / Refresh"
  // header was a second, redundant header above the step. Embedded mode drops
  // it; the refresh lives in the plan strip instead.
  const embedded = !!(opts && opts.embedded);
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
  let turnStartMaxId = 0;  // §17.1054 — durable-id watermark at turn start
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
  // §17.1052 — completion hand-off (the ✓ path and a reload of a finished
  // session both land here; the turn-loop path says the same in its own words).
  function completionText() {
    return "🎉 **Every step in this plan is done — the project is complete.** The deliverable has been compiled: open the Output tab to read it, or the Plan tab to review what changed along the way.";
  }
  function renderCompletionCard() {
    if (!stepHero || stepHero.querySelector(".assist-complete")) return;
    const jid = session?.job_id;
    const card = el("div", { class: "assist-complete row row-wrap" },
      el("strong", { text: "🎉 Job complete" }),
      el("span", { class: "dim", text: " — every step is done and the deliverable is compiled." }),
      el("span", { class: "spacer" }),
      jid ? el("a", { class: "btn btn-primary btn-sm", href: `#/job/${jid}/output`, text: "View output →" }) : null,
      jid ? el("a", { class: "btn btn-ghost btn-sm", href: `#/job/${jid}/plan`, text: "Review the plan" }) : null);
    stepHero.append(card);
    stepHero.classList.remove("hidden");
    // the hub's status pill was rendered once from the job row — tell it.
    if (jid) window.dispatchEvent(new CustomEvent("scaffold:job-status", { detail: { jobId: jid, status: "completed" } }));
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
    let res;
    try {
      res = await api.post(`/assist/${sessionId}/submit`, {
        node_key: nk, output, action: "submit", history: historyForGuide(),
      });
    } catch (e) {
      // §17.1052 — this path had no catch: a 409 (unclaimed step, finished
      // session) vanished and the ✓ press did nothing at all. Say what
      // happened, and for an unclaimed step claim it and try once more.
      const code = e?.detail?.error_code || e?.error_code || "";
      const msg = e?.detail?.message || e?.detail || e?.message || String(e);
      if (code === "must_claim_first" && !opts.retried) {
        await claimAndGuideNext();
        return submitEvidence(nk, output, { ...opts, quiet: true, retried: true });
      }
      appendBubble("assistant", "error", `⚠️ Couldn't submit that for ${nk}: ${msg}`);
      toast(`Submit failed: ${msg}`, "err");
      return false;
    }
    const st = res?.status;
    if (st === "committed") {
      // §17.1007 — name what was ACCOMPLISHED, not just that a counter moved.
      // `recap_done` is the §17.738 recap's DONE list, distilled from the whole
      // step transcript server-side and, until now, read by nothing on this
      // path. Absent on short steps that never built a recap — then this falls
      // back to the plain toast rather than inventing an achievement.
      const done = Array.isArray(res.recap_done) ? res.recap_done : [];
      if (done.length) {
        appendBubble("assistant", "committed",
          `✓ **Step ${nk} closed.** What you got done:\n\n` +
          done.map((d) => `- ${d}`).join("\n"));
      } else {
        toast(`✓ Step ${nk} committed.`, "ok");
      }
      // §17.1043 — the confirmed fix was applied to the steps ahead: say what
      // changed and where (the turn-loop path renders the same note server-side).
      if (res.reconciliation_note) appendBubble("assistant", "note", res.reconciliation_note);
      await load();
      if (session?.status !== "completed") { await claimAndGuideNext(); return true; }
      // §17.1052 — the last step just closed: say so, and hand the operator to
      // the deliverable instead of leaving a changed hero label to be noticed.
      appendBubble("assistant", "ask", completionText());
      renderCompletionCard();
      return true;
    }
    if (st === "verification_failed" || st === "step_incomplete" || st === "step_unverified") {
      const v = res.success_verdict || {};
      appendBubble("assistant", "verify",
        // §17.1016 — "unverified" is its own outcome: the verifier did not
        // judge the work bad, it could not tell, and saying "not successful"
        // to an operator who already said they were unsure is a third wrong
        // answer in a row.
        `⚠ Not committed — ${st === "step_unverified"
          ? "I couldn't verify this step from what you sent"
          : `the verifier judged this step **${st === "step_incomplete" ? "incomplete" : "not successful"}**`}.` +
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
  // §17.1055 — three tiers, not one row of nine: the primary verb, the four
  // the loop uses every few turns, and the escape hatches behind "⋯ More".
  const moreMenu = el("details", { class: "verbs-more" },
    el("summary", { class: "btn btn-sm btn-ghost", title: "More step actions", "aria-label": "More step actions", text: "⋯" }));
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
    // §17.1050 — stop and verify: everything the plan believes, one read-only
    // script, then repairs the operator confirms. The answer to "it keeps
    // fixing one thing at a time and never converges".
    verb("🩺 Verify state", "Check what is actually running against what the plan believes, then walk through the repairs", async () => {
      await runTurnStream({ command: "verify_state" });
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
    moreMenu
  );
  const moreBody = el("div", { class: "verbs-more-body" },
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
    // §17.1056 — undo a reopen from its pre-image (the engine keeps one for
    // every step a confirmed re-plan or state check reopens).
    verb("↶ Restore a reopened step", "Put a step that a re-plan or state check reopened back to done, with the evidence it had (refused once you've worked on it since)", async () => {
      const nk = (window.prompt("Which reopened step should go back to done? (step key, e.g. T3)") || "").trim();
      if (!nk) return;
      try {
        const res = await api.post(`/assist/${sessionId}/step/restore`, { node_key: nk });
        toast(`↶ ${res.node_key} restored to done.`, "ok");
        appendBubble("assistant", "note", `↶ Restored **${res.node_key}** to done from before its reopen (${res.evidence_chars} chars of evidence).`);
        await load();
      } catch (e) { toast(errText(e), "err"); }
    }),
    verb("⏸", "Pause / resume the session", async () => {
      const paused = session?.status === "paused";
      await api.post(`/assist/${sessionId}/${paused ? "resume" : "pause"}`);
      toast(paused ? "Session resumed." : "Session paused.", "ok");
      load();
    })
  );
  moreMenu.append(moreBody);
  // Close the overflow after a pick, and on a click anywhere else.
  moreBody.addEventListener("click", (e) => { if (e.target.closest("button")) moreMenu.open = false; });
  const onDocClick = (e) => { if (moreMenu.open && !moreMenu.contains(e.target)) moreMenu.open = false; };
  document.addEventListener("click", onDocClick);
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

  // §17.1096 — the "?" panel: a discoverable path that explains the controls
  // and the engine's own behaviours, built from the single ASSIST_HELP source.
  // Operator-initiated, so it never fights a background poll (no timer, no
  // modal): a plain toggled panel.
  const helpPanel = el("div", { class: "assist-help", hidden: true });
  function buildHelp() {
    const close = el("button", { class: "btn btn-sm btn-ghost assist-help-close", text: "✕", title: "Close help", "aria-label": "Close help", onClick: () => toggleHelp(false) });
    const secs = helpSections().map((s) =>
      el("section", { class: "assist-help-sec" },
        el("h3", { text: s.heading }),
        el("dl", { class: "assist-help-list" },
          ...s.items.flatMap((it) => [
            el("dt", { text: it.term }),
            el("dd", { text: it.plain }),
          ]))));
    const footer = el("div", { class: "assist-help-foot" },
      el("span", { text: "Turning on more of the engine? " }),
      el("a", { href: "#/capabilities", class: "assist-help-caps",
        text: "See what the engine can do →",
        title: "Optional parts of the engine, each with a step-by-step walkthrough to switch it on" }));
    mount(helpPanel,
      el("div", { class: "assist-help-head" },
        el("h2", { text: "How the Assistant works" }),
        close),
      el("p", { class: "sub", text: "Every button, and everything the engine does on its own. Hover a button any time to see the same note." }),
      ...secs,
      footer);
  }
  function toggleHelp(force) {
    const show = typeof force === "boolean" ? force : helpPanel.hidden;
    if (show && !helpPanel.childElementCount) buildHelp();
    helpPanel.hidden = !show;
    if (helpBtn) helpBtn.setAttribute("aria-expanded", show ? "true" : "false");
    if (show) helpPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  const helpBtn = el("button", { class: "btn btn-sm btn-ghost", text: "? Help", title: "What the buttons and the assistant do", "aria-expanded": "false", onClick: () => toggleHelp() });

  const header = el(
    "div",
    { class: "view-header" },
    el("div", {}, el("h1", { text: "Assistant" }), el("div", { class: "sub mono", text: shortId(sessionId) })),
    el("div", { class: "header-actions" }, helpBtn, el("a", { class: "btn btn-sm btn-ghost", href: "#/assist", text: "← Sessions" }), el("button", { class: "btn btn-sm", text: "Refresh", onClick: () => load() }))
  );

  const main = el("div", { class: "chat-main assist-main" + (embedded ? " embedded" : "") }, transcript, composer);
  // §17.845 — the editable living brief rides with the session (mounted once
  // the session tells us its job).
  const briefSlot = el("div", { class: "assist-brief-slot" });
  // §17.1055 — session / steps / environment / notes / facts and the brief
  // used to be five cards permanently under the chat. They are reference,
  // not the work: one folded row holds them, remembered per browser.
  const moreOpen = (() => { try { return storage.get(ASSIST_MORE_KEY) === "1"; } catch { return false; } })();
  const moreRow = el("details", { class: "assist-more" + (moreOpen ? "" : ""), open: moreOpen || null },
    el("summary", { text: "Session details — environment, pinned values, notes, brief" }),
    belowGrid, briefSlot);
  moreRow.addEventListener("toggle", () => { try { storage.set(ASSIST_MORE_KEY, moreRow.open ? "1" : "0"); } catch { /* private mode */ } });
  mount(container, embedded ? null : header, helpPanel, contractCard(null, session), stepHero, main, moreRow);
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
    const sel = el("select", { class: "step-picker", title: "Jump to a step — ✎ marks steps whose walkthrough is already written" });
    for (const st of steps) {
      const icon = STEP_ICON[st.step_status] || "○";
      const terminal = !["pending", "presented", "awaiting_input"].includes(st.step_status);
      const running = st.node_status === "running";
      // §17.1008 — `has_guidance` is selected by list_steps and was rendered by
      // nothing. It answers the question the jump-to picker actually raises:
      // does this step already have a walkthrough waiting, or will landing on
      // it mean generating one? (§17.901 keeps a stored walkthrough rather than
      // regenerating, so the distinction is real.)
      const label = `${icon} ${st.node_key} — ${(st.title || "").slice(0, 58)}`
        + (st.has_guidance ? "  ✎" : "")
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
    // never appears here. Count every terminal state (§17.938).
    const doneN = (sc.committed || 0) + (sc.done || 0)
      + (sc.skipped || 0) + (sc.handed_off || 0);
    const totalN = Object.values(sc).reduce((a, b) => a + b, 0);
    const cur = steps.find((x) => x.node_key === nk);
    const pos = planPosition(steps, nk);
    const total = pos.total || totalN;
    const done = pos.total ? pos.done : doneN;
    const pct = total ? Math.round((done / total) * 100) : 0;
    // §17.1007 — the phase badge: the near summit, six times a session instead
    // of once. Absent on short plans.
    const isTerminal = (st) => TERMINAL_STEP.has(st);
    let phaseTag = null;
    let stopHint = null;
    if (cur && cur.phase && cur.phase_total > 1) {
      const inPhase = steps.filter((x) => x.phase === cur.phase);
      const donePhase = inPhase.filter((x) => isTerminal(x.step_status)).length;
      phaseTag = el("span", {
        class: "tag phase-tag",
        title: `Phases follow the plan's dependencies — a boundary means the next group could not start until this one finished.`,
        text: `Phase ${cur.phase} of ${cur.phase_total} · ${cur.phase_pos} of ${cur.phase_size}`,
      });
      // §17.1007 — a sanctioned place to stop, offered only where it is true.
      if (cur.phase_pos === cur.phase_size && donePhase >= inPhase.length - 1
          && cur.phase < cur.phase_total) {
        stopHint = el("div", { class: "step-hero-stop" },
          `⏸ Finish this one and Phase ${cur.phase} is done — a clean place to stop. Your progress is saved; ✦ pick it up here whenever.`);
      }
    }
    const title = cur?.title || session.current_node_title || "";
    const short = (t, n = 64) => (t || "").length > n ? (t || "").slice(0, n - 1) + "…" : (t || "");
    // §17.1055 — the strip. Top: where in the plan (count + phase + bar),
    // the session state, the loop "?" and refresh. Middle: the step you are
    // on, framed by the step just finished and the one coming — the
    // three-item timeline is what makes "where am I" legible without reading
    // the whole picker. Bottom: the jump-to picker, compact and right-sized.
    stepHero.classList.remove("hidden");
    mount(
      stepHero,
      el("div", { class: "row plan-strip-top" },
        nk && pos.index
          ? el("span", { class: "plan-strip-count", text: `Step ${pos.index} of ${total}` })
          : el("span", { class: "plan-strip-count", text: total ? `${done} of ${total} done` : "Plan" }),
        phaseTag,
        el("span", { class: "spacer" }),
        el("span", { class: "plan-strip-done dim small", text: total ? `${done}/${total} done · ${pct}%` : "" }),
        statusBadge(session.status),
        nk ? el("button", {
            class: "btn btn-ghost btn-sm plan-strip-help",
            title: "How assist mode works: ✦ Guide me → do it on your machine → paste what happened → ✓ Done",
            "aria-label": "How assist mode works",
            text: "?",
            onClick: () => {
              const host = stepHero.parentNode;
              if (!host || host.querySelector(".assist-contract")) return;
              const card = contractCard(null, session, true);
              if (card) host.insertBefore(card, stepHero);
            },
          }) : null,
        embedded ? el("button", { class: "btn btn-ghost btn-sm", title: "Reload the conversation and step state", "aria-label": "Reload the conversation and step state", text: "↻", onClick: () => load() }) : null),
      el("div", { class: "plan-bar", title: `${done} of ${total} steps finished` },
        el("div", { class: "plan-bar-fill", style: `width:${pct}%` })),
      el("div", { class: "plan-strip-now" },
        pos.prev
          ? el("div", { class: "plan-step plan-prev", title: pos.prev.title || "" },
              el("span", { class: "plan-step-icon", text: STEP_ICON[pos.prev.step_status] || "✅" }),
              el("span", { class: "plan-step-key mono", text: pos.prev.node_key }),
              el("span", { class: "plan-step-title", text: short(pos.prev.title) }))
          : el("div", { class: "plan-step plan-prev faint", text: nk ? "start of plan" : "" }),
        nk
          ? el("div", { class: "plan-step plan-cur" },
              el("span", { class: "plan-step-icon", text: "📍" }),
              el("span", { class: "plan-step-key mono", text: nk }),
              el("span", { class: "plan-step-title", text: title }))
          : el("div", { class: "plan-step plan-cur dim", text: session.status === "completed" ? "Session complete 🎉 — the deliverable is in the Output tab" : "No step claimed — press ✦ Guide me to begin" }),
        pos.next
          ? el("div", { class: "plan-step plan-next", title: pos.next.title || "" },
              el("span", { class: "plan-step-icon", text: "○" }),
              el("span", { class: "plan-step-key mono", text: pos.next.node_key }),
              el("span", { class: "plan-step-title", text: short(pos.next.title) }))
          : el("div", { class: "plan-step plan-next faint", text: nk ? "last step of the plan" : "" })),
      stopHint,
      steps.length
        ? el("div", { class: "row row-wrap step-hero-nav" },
            el("span", { class: "dim small", text: "All steps:" }),
            renderStepPicker(nk))
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

  // §17.1011 — progressive disclosure for walkthrough bubbles.
  //
  // The live homelab job showed why: node T35's guidance was 4,817 chars over
  // NINE top-level sections, and the Run tab held 448 code blocks / 526 buttons
  // because every one of 41 steps rendered in full. The operator's actual
  // question — "what do I type right now" — was one line inside all of it.
  //
  // The engine already computes the answer: §17.741 leads every walkthrough
  // with `## 👉 Do this next` (one command), and §17.932 closes it with
  // `## ✅ Done when` (the finish line + how to advance). Those two stay open;
  // every other section collapses to a titled row the operator can open. This
  // HIDES nothing — the full runbook is one click away and still in the DOM —
  // it just stops the page shouting all nine sections at once.

  function guideBody(content, kind) {
    const { lead, sections } = splitGuideSections(content);
    const openRe = openSectionFor(kind);
    // Nothing to fold (short guidance, or no headings at all) — render as-is.
    const foldable = sections.filter((s) => !openRe.test(s.title));
    if (!foldable.length) return el("div", { class: "msg-body md", html: mdToHtml(content || "") });

    const open = sections.filter((s) => openRe.test(s.title));
    const openMd = [lead.trim(), ...open.map((s) => `## ${s.title}\n${s.body.join("\n")}`)]
      .filter(Boolean).join("\n\n");

    return el(
      "div",
      { class: "msg-body md" },
      el("div", { class: "guide-lead", html: mdToHtml(openMd) }),
      el(
        "div",
        { class: "guide-more" },
        ...foldable.map((s) =>
          el(
            "details",
            { class: "guide-section" },
            el("summary", { text: s.title + sectionCount(s.body.join("\n")) }),
            el("div", { class: "md", html: mdToHtml(s.body.join("\n")) })
          )
        )
      )
    );
  }

  function bubble(role, kind, content, ts) {
    const cls = role === "operator" ? "op" : role === "assistant" ? "as" : "sys";
    return el(
      "div",
      { class: `msg ${cls}` },
      el("div", { class: "msg-meta" }, el("span", { class: "msg-role", text: role }), kind && kind !== "message" ? el("span", { class: "msg-kind", text: kind }) : null, el("span", { class: "msg-time faint", text: ts ? timeAgo(ts) : "" })),
      // §17.1011 — only walkthrough-shaped turns fold; an operator paste, a
      // committed notice or a one-line note has no sections and must not gain
      // a disclosure row it does not need.
      (kind === "guide" || kind === "fix")
        ? guideBody(content, kind)
        : el("div", { class: "msg-body md", html: mdToHtml(content || "") })
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
    // §17.1011 — fold the BACKLOG. Measured on the live homelab session at
    // step 37 of 41, the Run tab held 526 buttons, 518 headings, 448 code
    // blocks and 1,319 links, because every prior step's full walkthrough was
    // mounted. The operator scrolls ~40 completed runbooks to reach the one
    // they are working. Recent turns stay open (the current step and the
    // exchange around it); everything older goes behind ONE row that says how
    // much is in there. Nothing is dropped — opening the row renders it all,
    // in order, and `stick()` still lands the view on the newest turn.
    // §17.1082 — the optimistic copy sendMessage() pushes into `turns`
    // (`_pending`) is painted by the pendingOps loop below, once. Drawing it
    // here too showed every operator message TWICE for the whole turn (the
    // reload at turn end replaced `turns` and hid it — so it read as "the
    // doubling is back" without ever being durable). Live, 2026-09-15.
    const { durable, pending } = transcriptPlan(turns, pendingOps);
    pendingOps = pending;
    const recent = durable.slice(-ASSIST_RECENT_TURNS);
    const older = durable.slice(0, durable.length - recent.length);
    if (older.length) {
      // Build the backlog body LAZILY. A closed <details> still mounts all of
      // its children: folding alone took the measured Run tab from 518 visible
      // headings to 199 but left buttons at 527, code blocks at 448 and links
      // at 1,319, because 40 walkthroughs were still in the DOM behind a
      // closed row. Rendering on first open keeps the page light until the
      // operator actually asks for the history.
      const backlogBody = el("div", { class: "transcript-backlog-body" });
      const backlog = el(
        "details",
        { class: "transcript-backlog" },
        el("summary", { text: `earlier in this session — ${older.length} messages` }),
        backlogBody
      );
      let backlogFilled = false;
      backlog.addEventListener("toggle", () => {
        if (!backlog.open || backlogFilled) return;
        backlogFilled = true;
        for (const t of older) backlogBody.append(bubble(t.role, t.kind, t.content, t.created_at));
      });
      mount(
        transcript,
        backlog,
        ...recent.map((t) => bubble(t.role, t.kind, t.content, t.created_at))
      );
    } else {
      mount(transcript, ...durable.map((t) => bubble(t.role, t.kind, t.content, t.created_at)));
    }
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
    for (const o of pendingOps) {
      transcript.append(bubble("operator", o.kind, o.content, o.created_at));
    }
    for (const e of ephemeralTail) {
      if (!ephemeralIsDurable(turns, e, turnStartMaxId) && e.content.trim()) {
        transcript.append(bubble("assistant", e.kind, e.content, e.at));
      }
    }
    stick();
  }

  // Lazily-fetched sidebar extras (checklist §17.707, environment §17.703) —
  // cached per load cycle; failures degrade to absent cards, never errors.
  let checklist = null;
  let environment = null;
  let systemMap = "";   // §17.1090 — the engine's joined view of the operator's machines
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
    const openN = checklist?.open_count ?? checkItems.filter((i) => !i.done).length;
    // §17.1055 — with nothing open this panel is a receipt, not a request:
    // one muted line, the list behind a click.
    if (!openN) {
      const doneList = el("details", { class: "checklist-done" },
        el("summary", { class: "side-title", text: `Nothing needed from you right now · ${checkItems.length} provided` }),
        ...checkItems.map((it) => el("div", { class: "side-check" },
          el("span", { class: "check-dot done", text: "✓" }),
          el("span", { class: "check-text dim" }, `${it.title || it.node_key}`,
            provided[it.node_key] ? el("span", { class: "faint", text: ` — ${provided[it.node_key]}` }) : null))));
      mount(checklistPanel, doneList);
      return;
    }
    mount(
      checklistPanel,
      el("div", { class: "side-title", text: `The engine needs from you (${openN} open)` }),
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
        const res = await api.req(`/assist/${sessionId}/env`, { method: "PUT", body: { substitutions: { [k]: v } } });
        toast(v ? `Pinned ${k} — walkthroughs will use it verbatim.` : `Cleared ${k}.`, "ok");
        // §17.1046 — a re-pin was applied to the steps ahead: say what changed.
        if (res?.reconciliation_note) appendBubble("assistant", "note", res.reconciliation_note);
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
          el("button", { class: "btn btn-ghost btn-sm bp-remove", text: "✕", title: "Clear this pin", "aria-label": "Clear this pin", onClick: () => putSub(k, "") }))),
      el("div", { class: "row bp-add-row" }, keyIn, valIn, el("button", { class: "btn btn-sm", text: "＋ Pin", onClick: add }))
    );
  }

  // Session / Steps / notes / facts / environment — the card row below the chat.
  function renderBelow() {
    if (!session) return;
    if (verbBtns["⏸"]) verbBtns["⏸"].textContent = session.status === "paused" ? "▶" : "⏸";
    if (session.status === "completed") renderCompletionCard();  // §17.1052 — a reload of a finished session lands on the hand-off too
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
        // §17.1090 — the SYSTEM MAP: what every walkthrough is told about your
        // machines (ids, names, addresses, MACs, the public entry point) and
        // the CONFLICTS in the records that only you can settle.
        systemMap ? el("details", { class: "side-map" },
          el("summary", { text: systemMap.includes("CONFLICTS") ? "System map — ⚠ conflicts to settle" : "System map (what the engine believes about your machines)" }),
          el("pre", { class: "side-map-pre", text: systemMap.replace(/^### [^\n]*\n/, "") })
        ) : null,
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
        api.get(`/assist/${sessionId}/env`).then((r) => { systemMap = (r && r.system_map) || ""; return r; }).catch(() => null),
        api.get(`/assist/${sessionId}/steps`).catch(() => null),
      ]);
      if (disposed) return;
      session = s;
      turns = t.turns || [];
      // §17.1011 — retire the onboarding card once the session says the
      // operator has actually done the loop. The mount-time check cannot fire:
      // `session` is still null when the shell is first mounted (it is only
      // assigned here), which is why a four-step "how assist mode works"
      // explainer was still on screen at step 37 of 41 on the live job.
      const _sc = s.step_counts || {};
      if (((_sc.committed || 0) + (_sc.done || 0)) > 0) {
        container.querySelector(".assist-contract")?.remove();
      }
      // Transient checklist/env fetch failures keep the last known value —
      // a blip must not blank the needs-from-you panel mid-session.
      if (cl !== null) checklist = cl;
      if (env !== null) environment = env?.environment ?? null;
      if (st !== null) steps = st?.steps || [];
      if (!embedded) header.querySelector(".sub").textContent = s.job_title || shortId(sessionId);
      if (!briefMounted && s.job_id) {
        briefMounted = true;
        mount(briefSlot, briefPanel(String(s.job_id)));
      }
      // §17.863 — a stashed-but-unresolved re-plan proposal re-renders after
      // any reload (the server now exposes it on the session read); resolving
      // it clears the slot.
      if (s.pending_replan) renderReplanProposal(s.pending_replan, { background: true });
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
  // §17.1093 — signatures the operator has ACTED on (applied/discarded) never
  // render again, and ones they closed with "Later" never RE-OPEN from a
  // background poll. The idle poll (§17.1090) re-opened the modal every 25 s
  // with {open:true} — "after accepting it just keeps appearing."
  const resolvedReplanSigs = new Set();
  const snoozedReplanSigs = new Set();
  // §17.1097 — sigs whose modal has already been auto-opened once this view.
  // First sighting → pop the modal; every later render (poll included) → chip.
  const announcedReplanSigs = new Set();

  const REPLAN_ACTION_COPY = {
    rewrite: { icon: "✎", label: "Reword", blurb: "the confirmed fix showed this instruction is wrong here; exact phrase replaced" },
    repair: { icon: "🩺", label: "Repair", blurb: "the state check found this result no longer holds; a step to put it right is inserted before the current one" },
    revise: { icon: "✏️", label: "revise", blurb: "rewrite this step's instructions" },
    drop: { icon: "🗑️", label: "drop", blurb: "remove this step from the plan" },
    reopen: { icon: "↩️", label: "reopen", blurb: "put this finished step back in play" },
  };

  function replanChangeRow(ch) {
    // §17.1097 — a real before→after diff, colour-coded, so the operator sees
    // exactly what the plan says now and what it would say instead.
    const meta = REPLAN_ACTION_COPY[ch.action] || { icon: "•", label: ch.action || "change", blurb: "" };
    const after = ch.proposed_change || ch.summary || ch.change || ch.reason || "";
    const before = ch.current_assumption || "";
    const isDrop = ch.action === "drop";
    const isReopen = ch.action === "reopen";
    const diff = el("div", { class: "replan-diff" },
      el("div", { class: "replan-side replan-before" },
        el("span", { class: "replan-tag", text: isReopen ? "Was" : "Now" }),
        el("span", { class: "replan-text", text: before || (isReopen ? "marked finished" : "this step, as written") })),
      el("div", { class: "replan-arrow", text: "→" }),
      el("div", { class: "replan-side replan-after" + (isDrop ? " replan-side-drop" : "") },
        el("span", { class: "replan-tag", text: isDrop ? "Remove" : isReopen ? "Back to" : "After" }),
        el("span", { class: "replan-text", text: isDrop ? "dropped from the plan" : isReopen ? "in play again" : (after || "(updated)") })));
    return el("div", { class: "replan-change" },
      el("div", { class: "replan-change-head" },
        el("span", { class: "replan-act act-" + (ch.action || "other"), text: `${meta.icon} ${meta.label}` }),
        el("span", { class: "mono replan-node", text: ch.node_key || "" }),
        meta.blurb ? el("span", { class: "dim small", text: `— ${meta.blurb}` }) : null),
      diff);
  }

  // The outcome popup — brief, centred, with the REAL counts (§17.865).
  const ackPopup = (icon, title, body) => {
    const overlay = el("div", { class: "ack-overlay", role: "status", "aria-live": "polite" },   // §17.1118 — announced, not focus-stealing
      el("div", { class: "card ack-card" },
        el("span", { class: "ack-icon", text: icon }),
        el("strong", { text: title }),
        body ? el("p", { class: "dim", text: body }) : null));
    overlay.addEventListener("click", () => overlay.remove());
    document.body.append(overlay);
    setTimeout(() => overlay.remove(), 2600);
  };

  function renderReplanProposal(p, { open = false, background = false } = {}) {
    const changes = p.proposals || [];
    if (!changes.length) { mount(replanSlot); return; }
    const sig = JSON.stringify(changes.map((c) => [c.node_key, c.action, c.proposed_change]));
    const d = replanRenderDecision(sig, { announced: announcedReplanSigs,
                                          resolved: resolvedReplanSigs, snoozed: snoozedReplanSigs });
    lastReplanSig = sig;
    if (!d.show) { mount(replanSlot); return; }
    const shouldOpen = d.openModal;
    // Mark announced NOW so no later render (the 25 s poll included) re-pops it.
    announcedReplanSigs.add(sig);

    const counts = changes.reduce((a, c) => { a[c.action] = (a[c.action] || 0) + 1; return a; }, {});
    const countText = ["revise", "drop", "reopen", "rewrite", "repair"]
      .filter((k) => counts[k])
      .map((k) => `${counts[k]} to ${k}`).join(" · ");

    let overlay = null, dialog = null;
    const closeModal = () => {
      if (dialog) { dialog.close(); dialog = null; }      // §17.1118 — restore focus
      if (overlay) { overlay.remove(); overlay = null; }
    };

    const resolve = async (decision) => {
      const n = changes.length;
      resolvedReplanSigs.add(sig);   // §17.1093 — never resurface this exact set
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
          const rw = (res.rewritten || []).length;
          const rp = (res.repaired || []).length;
          ackPopup("✅", "Plan updated",
            `${rev} step(s) revised, ${drop} dropped${rw ? `, ${rw} reworded` : ""}${rp ? `, ${rp} repair step(s) added` : ""}. Continuing on the revised plan…`);
          toast(`Plan updated — ${rev} revised, ${drop} dropped${rw ? `, ${rw} reworded` : ""}${rp ? `, ${rp} repairs` : ""}.`, "ok");
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
      laterBtn.addEventListener("click", () => { snoozedReplanSigs.add(sig); closeModal(); renderChip(); });

      overlay = el("div", { class: "modal-overlay" },
        el("div", { class: "card modal-card replan-modal" },
          el("div", { class: "modal-head" },
            el("strong", { text: "📋 This changes the plan" }),
            el("button", { class: "btn btn-ghost btn-sm", text: "✕", "aria-label": "Close",
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
      // §17.1118 (ledger U-11) — a real dialog: role, focus moved in, Tab
      // kept inside, Escape closes, focus returned to the Review chip.
      dialog = openDialog(overlay.firstElementChild, {
        label: "Plan change proposal", onClose: () => { closeModal(); renderChip(); },
      });
    };

    const renderChip = () => {
      const chip = el("div", { class: "replan-chip" },
        el("span", { text: `📋 ${changes.length} proposed plan change${changes.length === 1 ? "" : "s"}` }),
        el("button", { class: "btn btn-sm btn-primary", text: "Review", onClick: openModal }));
      mount(replanSlot, chip);
    };

    renderChip();
    if (shouldOpen) openModal();
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
    let sawDone = false, stoppedByUser = false;   // §17.1082
    abort = new AbortController();
    ephemeralTail = [];  // §17.870 — fresh turn, fresh tail
    // §17.871 — small slack for client/server clock skew; a capture stamped
    // slightly "before" our local start must still count as ours.
    turnStartedAt = new Date(Date.now() - 15000).toISOString();
    // §17.1054 — the dedupe key is the durable id watermark, not the clock:
    // "captured during this turn" = id above the newest durable turn at turn
    // start. The time cutoff compared the BROWSER's clock to the server's
    // created_at; a browser more than 15 s ahead made every server capture
    // look older than the turn, nothing retired, and every answer showed
    // twice (live, 2026-09-13).
    turnStartMaxId = maxTurnId(turns);
    // §17.1082 — the status line is a LIVE indicator, not a caption: a
    // spinner, the last thing the engine said it was doing, and a clock that
    // ticks every second from the turn's start. Live, 2026-09-15: a 105 s
    // research pass (two silent model retries inside) sat behind one static
    // dim line and read as a dead page. The server's pulse frames prove the
    // run is alive; the clock proves the page is.
    let statusEl = null, statusText = "", statusTimer = null, lastSign = Date.now();
    const turnT0 = Date.now();
    const fmtElapsed = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`; };
    const paintStatus = () => {
      if (!statusEl) return;
      const quiet = Date.now() - lastSign;
      let tail = ` · working ${fmtElapsed(Date.now() - turnT0)}`;
      if (quiet > 45000) tail += ` — no word from the engine for ${fmtElapsed(quiet)}; if this keeps up, press ■ Stop and resend`;
      statusEl.querySelector(".status-text").textContent = statusText + tail;
      // §17.1117 (ledger U-8) — the dot the help panel describes. It exists
      // now: live while frames arrive, quiet after 45 s, stale after 2 min.
      const dot = statusEl.querySelector(".assist-pulse");
      if (dot) dot.dataset.state = pulseState(quiet);
    };
    const setStatusLine = (t) => {
      if (!statusEl) {
        statusEl = el("div", { class: "msg sys" },
          el("div", { class: "msg-body dim" },
            el("span", { class: "assist-pulse", dataset: { state: "live" }, title: "Blinks each time the engine reports progress" }),
            el("span", { class: "spin", style: "vertical-align:middle;margin-right:8px" }), el("span", { class: "status-text" })));
        transcript.append(statusEl);
        statusTimer = setInterval(paintStatus, 1000);
      }
      statusText = t;
      lastSign = Date.now();
      paintStatus();
      stick();
    };
    const pulse = () => {
      lastSign = Date.now();
      paintStatus();
      // restart the blink so every frame is a visible beat, not a steady glow
      const dot = statusEl && statusEl.querySelector(".assist-pulse");
      if (dot) { dot.classList.remove("beat"); void dot.offsetWidth; dot.classList.add("beat"); }
    };
    const clearStatusLine = () => {
      if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
      if (statusEl) { statusEl.remove(); statusEl = null; }
    };
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
          case "assist_turn_pulse":
            pulse();
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
            sawDone = true;
            break;
          case "error": {
            sawDone = true;
            clearStatusLine();
            const rec = restartRecovery(turns, data?.detail || "");
            if (rec.kind === "answered") {
              // the restart killed a run whose work had already landed above
              appendBubble("assistant", "note", "The engine restarted, but your last step is safe — its result is above. Continue when ready.");
            } else if (rec.kind === "restore") {
              // give the operator their input back instead of "retype it"
              if (!composerText.value.trim()) composerText.value = rec.text;
              appendBubble("assistant", "note", "⚠ The engine restarted mid-turn. Your message is restored in the box below — press Send to resume (nothing was lost).");
            } else {
              appendBubble("assistant", "note", `⚠ ${data?.detail || "turn error"}`);
            }
            break;
          }
          default:
            break;
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") toast(errText(e), "err");
      else stoppedByUser = true;
    } finally {
      guiding = false;
      abort = null;
      sendBtn.disabled = false;
      guideBtn.textContent = "✦ Guide me";
      clearStatusLine();
      if (live) live.classList.remove("streaming");
      // §17.1082 — the stream closed without a terminal frame and nobody
      // pressed Stop: the turn is most likely still running server-side
      // (the tail follows a detached run). Say so, and re-attach if it is.
      if (!sawDone && !stoppedByUser && !disposed) {
        appendBubble("assistant", "note", "⚠ The connection to the engine closed before this turn finished. Checking whether it is still running…");
        resumeChecked = false;
        setTimeout(() => { if (!disposed) maybeResumeActiveTurn(true); }, 1500);
      }
      load();
    }
  }

  // §17.869 — after any reload, re-attach to a still-running turn. The tail
  // replays what was missed; the operator never loses an in-flight turn to
  // navigation again.
  let resumeChecked = false;
  async function maybeResumeActiveTurn(afterDrop = false) {
    if (guiding || resumeChecked) return;
    resumeChecked = true;
    try {
      const act = await api.get(`/assist/${sessionId}/message/active`);
      if (act?.run_id && !guiding) {
        toast("Re-attaching to the turn that was still running…", "");
        await runTurnStream(null, act.run_id);
      } else if (afterDrop) {
        appendBubble("assistant", "note", "The turn is not running any more. Its result, if any, is in the transcript above — otherwise send your message again.");
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

  // §17.1090 — a re-plan proposal staged by a FACT (§17.1089) lands in the
  // background after the turn's reload; poll the session while idle so it
  // surfaces within half a minute instead of on the next turn. The proposal
  // renderer dedupes by signature, so a repeat read draws nothing twice.
  const idlePoll = setInterval(async () => {
    if (disposed || guiding || document.hidden) return;   // §17.1118 — skip hidden tabs
    try {
      const s = await api.get(`/assist/${sessionId}`);
      if (disposed || guiding) return;
      if (s && s.pending_replan) renderReplanProposal(s.pending_replan, { open: true, background: true });
    } catch { /* transient — the next tick retries */ }
  }, 25000);

  return () => {
    disposed = true;
    clearInterval(idlePoll);
    document.removeEventListener("selectionchange", onSelChange);  // §17.890
    document.removeEventListener("click", onDocClick);  // §17.1055
    if (abort) abort.abort();
  };
}

export default function assist(container, params) {
  if (params && params.sessionId) return renderChat(container, params.sessionId);
  return renderPicker(container);
}
