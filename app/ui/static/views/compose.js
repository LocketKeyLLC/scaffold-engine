// New idea — ONE box, two verbs. "Build it" turns what you typed into a job
// (POST /ideate/start, ms; refinement runs server-side and the job page opens
// on the approval gate). "Ask the engine" sends the same text to the engine's
// native chat (/v1, §17.793 — triage, status questions, /go) and shows the
// conversation right here. The separate Chat page is folded in (UX overhaul,
// 2026-09-27): two doors for "type what you want" was one too many.
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount, mdToHtml, stickyScroll, selectionWithin } from "../util.js";
import { toast } from "../components.js";

// §17.818 — the chat model id comes from GET /v1/models (was a literal).
let MODEL = "scaffold-engine";
// Hide the confirm-card marker line from display but keep it in history (it's
// how the engine reconstructs a pending action on the next turn).
const MARKER_RE = /^[ \t]*(?:\[nlc\]:[ \t]*|<!--[ \t]*)NL_CONFIRM:[A-Za-z0-9_-]+[ \t]*(?:-->)?[ \t]*$/gm;
export function forDisplay(s) {
  return (s || "").replace(MARKER_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

// §17.854 (audit G8) — the conversation survives navigation, per tab.
const CHAT_STORE_KEY = "scaffold_chat_history";
function loadChatHistory() {
  try { return JSON.parse(sessionStorage.getItem(CHAT_STORE_KEY)) || []; }
  catch { return []; }
}
function saveChatHistory(messages) {
  try { sessionStorage.setItem(CHAT_STORE_KEY, JSON.stringify(messages)); }
  catch { /* quota / private mode — degrade to in-memory only */ }
}

// §17.1007 — the first chip is a COMPLETE brief so the anchor points at the
// level of detail the engine rewards; the rest are running starts.
export const STARTERS = [
  ["See a full example",
    "Deploy a Prometheus + Grafana monitoring stack on my Proxmox host (Debian 12, 32GB RAM, already running 4 LXC containers). " +
    "It should scrape node-exporter on three existing VMs, keep 30 days of metrics, and come up automatically after a host reboot. " +
    "I want a runbook I can follow myself plus the compose file — I have shell access but I'm not confident with systemd units."],
  ["CLI tool", "A command-line tool that "],
  ["Home-lab service", "Deploy and configure "],
  ["Data pipeline", "A pipeline that ingests "],
  ["Research + report", "Research and write a practical guide to "],
  ["What's running?", "What's running right now?"],
];

export default function compose(container) {
  // §17.815 — native chat rides /v1, admin-only by design; the Ask verb
  // simply isn't offered to a scoped key.
  const isAdmin = api.principal()?.is_admin !== false;
  if (isAdmin) api.get("/v1/models").then((r) => { const id = r?.data?.[0]?.id; if (id) MODEL = id; }).catch(() => {});
  let busy = false;
  let streaming = false;
  let abort = null;
  let disposed = false;
  const messages = loadChatHistory();

  const idea = el("textarea", {
    class: "input compose-idea",
    rows: "7",
    placeholder: "What do you want to build? Hardware, constraints, what you already have running, and what you want at the end.",
    "aria-label": "Describe what you want to build",
  });
  const domain = el("select", { class: "input input-sm", "aria-label": "Domain" }, el("option", { value: "", text: "Domain: auto-detect" }));
  api.domains().then((ds) => ds.forEach((d) => domain.append(el("option", { value: d, text: `Domain: ${d}` }))));
  const status = el("div", { class: "compose-status", role: "status" });
  const buildBtn = el("button", { class: "btn btn-primary", text: "Build it →", title: "The engine refines this into a brief, asks what it needs to know, then plans it for your approval." });
  const askBtn = isAdmin ? el("button", { class: "btn btn-ghost", text: "Ask the engine", title: "A question, a status check, or /go — answered here, no job created." }) : null;

  async function build() {
    if (busy) return;
    const text = idea.value.trim();
    if (!text) { status.textContent = "Describe your idea first."; idea.focus(); return; }
    busy = true; buildBtn.disabled = true; status.textContent = "";
    const label = buildBtn.textContent;
    buildBtn.textContent = "Starting…";
    try {
      const res = await api.post("/ideate/start", { idea: text, domain: domain.value || null });
      const jobId = res && res.job_id;
      toast("Idea submitted — the engine is refining it. Approve it when it's ready.", "ok");
      // §17.895 — land on the job page: it opens on the approval gate while
      // the brief refines (the live "refining…" polling state).
      router.navigate(jobId ? `/job/${jobId}` : "/");
    } catch (e) {
      busy = false; buildBtn.disabled = false; buildBtn.textContent = label;
      toast(`Could not submit: ${e.detail || e.message}`, "err");
    }
  }

  // ── Ask: the native chat, in place ──────────────────────────────────
  const transcript = el("div", { class: "chat-transcript compose-transcript" });
  const stick = stickyScroll(transcript);
  let renderDeferred = false;
  const onSelChange = () => { if (renderDeferred && !selectionWithin(transcript)) renderTranscript(); };
  document.addEventListener("selectionchange", onSelChange);
  const clearBtn = el("button", { class: "btn btn-sm btn-ghost", text: "Clear conversation", onClick: () => {
    if (streaming && abort) abort.abort();
    messages.length = 0; saveChatHistory(messages); renderTranscript();
  } });
  const convo = el("div", { class: "card compose-convo", hidden: true },
    el("div", { class: "row compose-convo-head" }, el("span", { class: "dim", text: "Conversation with the engine" }), el("span", { class: "spacer" }), clearBtn),
    transcript);

  function bubble(role, content, live) {
    const cls = role === "user" ? "op" : "as";
    return el("div", { class: `msg ${cls}${live ? " streaming" : ""}` },
      el("div", { class: "msg-meta" }, el("span", { class: "msg-role", text: role === "user" ? "you" : "engine" })),
      el("div", { class: "msg-body md", html: mdToHtml(forDisplay(content)) || (live ? '<span class="spin"></span>' : "") }));
  }
  function renderTranscript() {
    if (selectionWithin(transcript)) { renderDeferred = true; return; }   // §17.890
    renderDeferred = false;
    convo.hidden = !messages.length;
    mount(transcript, ...messages.map((m) => bubble(m.role, m.content, false)));
    stick();
  }

  async function ask() {
    const text = idea.value.trim();
    if (!text || streaming) return;
    idea.value = "";
    messages.push({ role: "user", content: text });
    saveChatHistory(messages);
    renderTranscript();
    streaming = true;
    askBtn.disabled = true; askBtn.textContent = "…";
    abort = new AbortController();
    const live = bubble("assistant", "", true);
    const body = live.querySelector(".msg-body");
    transcript.append(live); stick();
    let acc = "";
    let renderPending = false;   // §17.854 G8 — rAF-coalesced repaint
    const scheduleRender = () => {
      if (renderPending) return;
      renderPending = true;
      requestAnimationFrame(() => {
        renderPending = false;
        if (disposed) return;
        if (!selectionWithin(transcript)) body.innerHTML = mdToHtml(forDisplay(acc));
        stick();
      });
    };
    try {
      for await (const { data } of api.stream("/v1/chat/completions", {
        body: { model: MODEL, stream: true, messages: messages.map((m) => ({ role: m.role, content: m.content })) },
        signal: abort.signal,
      })) {
        if (disposed) break;
        if (data === "[DONE]" || (typeof data === "string" && data.trim() === "[DONE]")) break;
        const piece = data && data.choices && data.choices[0] && data.choices[0].delta && data.choices[0].delta.content;
        if (piece) { acc += piece; scheduleRender(); }
      }
      messages.push({ role: "assistant", content: acc });
      saveChatHistory(messages);
    } catch (e) {
      if (e.name !== "AbortError") {
        const msg = e.status === 404
          ? "Asking the engine is switched off (NATIVE_OPENAI_ENABLED). Building still works."
          : e.detail || e.message || "stream error";
        body.innerHTML = mdToHtml(`⚠ ${msg}`);
        if (acc) { messages.push({ role: "assistant", content: acc }); saveChatHistory(messages); }
      }
    } finally {
      streaming = false; abort = null;
      askBtn.disabled = false; askBtn.textContent = "Ask the engine";
      live.classList.remove("streaming");
      renderTranscript();
      idea.focus();
    }
  }

  buildBtn.addEventListener("click", build);
  if (askBtn) askBtn.addEventListener("click", ask);
  // ⌘/Ctrl+Enter builds from the textarea; Shift+⌘/Ctrl+Enter asks.
  idea.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); if (e.shiftKey && askBtn) ask(); else build(); }
  });

  mount(
    container,
    el("div", { class: "view-header" },
      el("div", {}, el("h1", { text: "New idea" }),
        el("div", { class: "sub", text: isAdmin
          ? "Describe it in plain words. Build it makes a plan you approve; Ask the engine answers a question or checks on a job."
          : "Describe it in plain words — the engine turns it into a plan you approve." }))),
    el("div", { class: "card card-pad compose-card" },
      idea,
      el("div", { class: "starter-chips compose-starters" },
        ...STARTERS.map(([label, fill]) =>
          el("button", { class: "btn btn-sm starter-chip", text: label,
            onClick: () => { idea.value = fill; idea.focus(); idea.setSelectionRange(fill.length, fill.length); } }))),
      el("div", { class: "row row-wrap compose-actions" }, buildBtn, askBtn, status, el("span", { class: "spacer" }), domain),
      // §17.1007 — what vagueness costs, said where it is being paid.
      el("p", { class: "compose-cost dim",
        text: "Detail pays for itself: what you leave out here is what the engine stops and asks you at the approval gate." })),
    convo
  );
  renderTranscript();
  idea.focus();

  return () => {
    disposed = true;
    document.removeEventListener("selectionchange", onSelChange);
    if (abort) abort.abort();
  };
}
