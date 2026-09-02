// Native in-SPA "New idea" composer. Posts to /ideate/start (async Phase-1
// kickoff — returns a job_id in ms while refinement runs server-side), then
// lands on the dashboard where the refining job shows under Active work and
// the Approve action surfaces once it reaches awaiting_confirmation.
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount } from "../util.js";
import { toast } from "../components.js";

// §17.818 — domains come from GET /meta/domains (single source; the audit
// found this list duplicated across four views + the web template).

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
    el("option", { value: "", text: "Auto-detect from idea" })
  );
  api.domains().then((ds) => ds.forEach((d) => domain.append(el("option", { value: d, text: d }))));
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
      // §17.895 — land in the JOB HUB, whose Overview tab embeds the approval
      // gate for pre-approval statuses (job_hub.GATE_STATUSES), including the
      // live "refining…" polling state this used to reach directly.
      //
      // This line pointed at `/approvals/:id` — a route §17.859 RETIRED when it
      // folded the gate into the hub. The router has no 2-segment /approvals
      // pattern, so every idea submission fell through to `setNotFound`, which
      // silently renders the Dashboard: the operator submitted an idea, landed
      // somewhere else with the URL still reading #/approvals/<uuid>, and had
      // no path onward. That is the missing "idea → approve" progression — and
      // it failed invisibly because not-found is a fallback, not an error.
      router.navigate(jobId ? `/job/${jobId}` : "/");
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
      el(
        "div",
        { class: "starter-chips compose-starters" },
        ...[
          ["CLI tool", "A command-line tool that "],
          ["Home-lab service", "Deploy and configure "],
          ["Data pipeline", "A pipeline that ingests "],
          ["Research + report", "Research and write a practical guide to "],
        ].map(([label, fill]) =>
          el("button", {
            class: "btn btn-sm starter-chip",
            text: label,
            onClick: () => { idea.value = fill; idea.focus(); idea.setSelectionRange(fill.length, fill.length); },
          })
        )
      ),
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
