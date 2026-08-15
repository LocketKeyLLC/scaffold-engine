// Native in-SPA "New idea" composer. Posts to /ideate/start (async Phase-1
// kickoff — returns a job_id in ms while refinement runs server-side), then
// lands on the dashboard where the refining job shows under Active work and
// the Approve action surfaces once it reaches awaiting_confirmation.
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount } from "../util.js";
import { toast } from "../components.js";

// Mirrors app/web/routes.py::_ALLOWED_DOMAINS (no API exposes this list).
// "" = auto-detect from the idea text.
const DOMAINS = ["prompt", "rag", "llm", "spec", "eng", "eng_design"];

export default function compose(container) {
  let busy = false;

  const idea = el("textarea", {
    class: "input compose-idea",
    rows: "10",
    placeholder: "Describe what you want the workflow to produce…",
  });
  const domain = el(
    "select",
    { class: "input" },
    el("option", { value: "", text: "Auto-detect from idea" }),
    ...DOMAINS.map((d) => el("option", { value: d, text: d }))
  );
  const status = el("div", { class: "compose-status", role: "status" });
  const submit = el("button", { class: "btn btn-primary", text: "Submit idea" });

  async function send() {
    if (busy) return;
    const text = idea.value.trim();
    if (!text) {
      status.textContent = "Describe your idea first.";
      idea.focus();
      return;
    }
    busy = true;
    submit.disabled = true;
    status.textContent = "";
    const label = submit.textContent;
    submit.textContent = "Submitting…";
    try {
      const res = await api.post("/ideate/start", { idea: text, domain: domain.value || null });
      const jobId = res && res.job_id;
      toast("Idea submitted — refining now. Approve it here when it’s ready.", "ok");
      // Deep-link to the approval detail: it shows a live "refining…" state and
      // polls until the feasibility assessment is ready to approve. Fall back to
      // the dashboard if no id came back.
      router.navigate(jobId ? `/approvals/${jobId}` : "/");
    } catch (e) {
      busy = false;
      submit.disabled = false;
      submit.textContent = label;
      toast(`Could not submit: ${e.detail || e.message}`, "err");
    }
  }

  submit.addEventListener("click", send);
  // ⌘/Ctrl+Enter submits from the textarea.
  idea.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send();
  });

  mount(
    container,
    el(
      "div",
      { class: "view-header" },
      el(
        "div",
        {},
        el("h1", { text: "New idea" }),
        el("div", {
          class: "sub",
          text: "Describe a workflow to build — Phase 1 refines it into a brief and assesses feasibility.",
        })
      )
    ),
    el(
      "div",
      { class: "card card-pad compose-card" },
      el("label", { class: "compose-label", text: "Idea" }),
      idea,
      el("label", { class: "compose-label", text: "Domain" }),
      domain,
      el("div", {
        class: "compose-hint faint",
        text: "Auto-detect works for most ideas — override only if you know the target domain.",
      }),
      el("div", { class: "compose-actions row" }, submit, status)
    )
  );
  idea.focus();
}
