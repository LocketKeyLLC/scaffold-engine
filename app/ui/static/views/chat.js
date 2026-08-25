// Native chat over the engine's OpenAI-compatible surface (§17.793). Streams
// POST /v1/chat/completions (X-API-Key via api.stream) and renders the engine's
// own dispatch — conversational triage, NL commands + confirm-cards, /go
// synthesis, and the /confirm build auto-chain — with no OWUI or pipeline in the
// loop. Conversation state lives in messages[] (the same stateless model the
// engine reads, so confirm-card follow-ups work across turns).
import * as api from "../api.js";
import { el, mount, mdToHtml } from "../util.js";

// §17.818 — the chat model id comes from GET /v1/models (was a literal).
let MODEL = "scaffold-engine";
api.get("/v1/models").then((r) => {
  const id = r?.data?.[0]?.id;
  if (id) MODEL = id;
}).catch(() => {});
// Hide the confirm-card marker line from display but keep it in history (it's
// how the engine reconstructs a pending action on the next turn).
const MARKER_RE = /^[ \t]*(?:\[nlc\]:[ \t]*|<!--[ \t]*)NL_CONFIRM:[A-Za-z0-9_-]+[ \t]*(?:-->)?[ \t]*$/gm;

function forDisplay(s) {
  return (s || "").replace(MARKER_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

export default function chat(container) {
  let disposed = false;
  let streaming = false;
  let abort = null;
  const messages = []; // {role, content} — OpenAI history (raw; markers kept)

  const transcript = el("div", { class: "chat-transcript" });
  const input = el("textarea", {
    class: "chat-input",
    placeholder: "Describe a build, ask for job status, or type /go…",
    rows: "2",
  });
  const sendBtn = el("button", { class: "btn btn-sm btn-primary", text: "Send", onClick: () => send() });
  const composer = el(
    "div",
    { class: "chat-composer" },
    input,
    el(
      "div",
      { class: "composer-actions" },
      el("span", { class: "faint", text: "⌘/Ctrl+↵ to send" }),
      el("span", { class: "spacer" }),
      sendBtn
    )
  );
  const header = el(
    "div",
    { class: "view-header" },
    el("div", {}, el("h1", { text: "Chat" }), el("div", { class: "sub", text: "Talk to the engine natively — triage, commands, and builds over /v1" })),
    el("div", { class: "header-actions" }, el("button", {
      class: "btn btn-sm btn-ghost",
      text: "New chat",
      onClick: () => {
        if (streaming && abort) abort.abort();
        messages.length = 0;
        render();
        input.focus();
      },
    }))
  );
  mount(container, header, el("div", { class: "chat-main" }, transcript, composer));

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      send();
    }
  });

  function bubble(role, content, live) {
    const cls = role === "user" ? "op" : "as";
    return el(
      "div",
      { class: `msg ${cls}${live ? " streaming" : ""}` },
      el("div", { class: "msg-meta" }, el("span", { class: "msg-role", text: role === "user" ? "you" : "engine" })),
      el("div", { class: "msg-body md", html: mdToHtml(forDisplay(content)) || (live ? '<span class="spin"></span>' : "") })
    );
  }

  function render() {
    if (!messages.length) {
      mount(transcript, el("div", { class: "empty-state small" }, el("p", {
        text: "Describe what you want to build, ask for job status (\"what's running\"), or type /go to launch.",
      })));
      return;
    }
    mount(transcript, ...messages.map((m) => bubble(m.role, m.content, false)));
    transcript.scrollTop = transcript.scrollHeight;
  }

  async function send() {
    const text = input.value.trim();
    if (!text || streaming) return;
    input.value = "";
    messages.push({ role: "user", content: text });
    render();

    streaming = true;
    sendBtn.disabled = true;
    sendBtn.textContent = "…";
    abort = new AbortController();
    const live = bubble("assistant", "", true);
    const body = live.querySelector(".msg-body");
    transcript.append(live);
    transcript.scrollTop = transcript.scrollHeight;

    let acc = "";
    try {
      for await (const { data } of api.stream("/v1/chat/completions", {
        body: { model: MODEL, stream: true, messages: messages.map((m) => ({ role: m.role, content: m.content })) },
        signal: abort.signal,
      })) {
        if (disposed) break;
        if (data === "[DONE]" || (typeof data === "string" && data.trim() === "[DONE]")) break;
        const delta = data && data.choices && data.choices[0] && data.choices[0].delta;
        const piece = delta && delta.content;
        if (piece) {
          acc += piece;
          body.innerHTML = mdToHtml(forDisplay(acc));
          transcript.scrollTop = transcript.scrollHeight;
        }
      }
      messages.push({ role: "assistant", content: acc });
    } catch (e) {
      if (e.name !== "AbortError") {
        const msg = e.status === 404
          ? "Native chat is off. Set NATIVE_OPENAI_ENABLED=true and restart the orchestrator — or check [Settings](#/settings) for the effective config."
          : e.detail || e.message || "stream error";
        body.innerHTML = mdToHtml(`⚠ ${msg}`);
        if (acc) messages.push({ role: "assistant", content: acc });
      }
    } finally {
      streaming = false;
      sendBtn.disabled = false;
      sendBtn.textContent = "Send";
      live.classList.remove("streaming");
      abort = null;
      render();
      input.focus();
    }
  }

  render();
  input.focus();

  return () => {
    disposed = true;
    if (abort) abort.abort();
  };
}
