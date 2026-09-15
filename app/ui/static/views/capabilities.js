// §17.1081 — the engine's optional capabilities, each with its DETECTED
// status and a "Walk me through it" button that opens the recipe as an
// ordinary job (Phase 1 refine → approve with Assist → guided steps). The
// engine walks the operator through its own setup; nobody writes a page.
import * as api from "../api.js";
import * as router from "../router.js";
import { el, mount } from "../util.js";
import { errorPanel, loading, toast } from "../components.js";

const STATUS_LABEL = {
  on: "On",
  off: "Off",
  blocked: "Needs another capability first",
  in_progress: "Walkthrough open",
  manual: "Confirmed only by walkthrough",
};
// Reuse the job-status badge palette: on → completed (green), in_progress →
// running (blue), blocked/manual → awaiting (amber), off → plain.
const STATUS_CLASS = { on: "st-completed", in_progress: "st-running", blocked: "st-blocked", manual: "st-awaiting_reply", off: "plain" };

export default function capabilities(container) {
  let disposed = false;
  const body = el("div", {});
  mount(container,
    el("div", { class: "view-header" },
      el("h1", { text: "Capabilities" }),
      el("div", { class: "sub", text: "Optional parts of the engine that ship switched off. Each one can walk you through turning it on, step by step, checking your output as you go." })),
    body);

  async function load() {
    mount(body, loading("Checking what is on…"));
    let res;
    try {
      res = await api.get("/setup/recipes");
    } catch (e) {
      if (!disposed) mount(body, errorPanel(e, load));
      return;
    }
    if (disposed) return;
    const byId = Object.fromEntries((res.recipes || []).map((r) => [r.id, r]));
    mount(body, el("div", { class: "grid grid-2" }, ...(res.recipes || []).map((r) => card(r, byId))));
  }

  function card(r, byId) {
    const badge = el("span", { class: `badge ${STATUS_CLASS[r.status] || "plain"}`, text: STATUS_LABEL[r.status] || r.status });
    const head = el("div", { style: "display:flex;justify-content:space-between;gap:12px;align-items:flex-start" },
      el("h3", { style: "margin:0;font-size:var(--fs-md)", text: r.title }), badge);
    const parts = [head,
      el("p", { style: "margin:8px 0 4px", text: r.summary }),
      el("p", { class: "sub", style: "margin:0 0 6px", text: "Why it is off: " + r.why_off }),
      el("p", { class: "sub", style: "margin:0 0 6px", text: r.status_detail || "" }),
    ];
    if (r.requires && r.requires.length) {
      parts.push(el("p", { class: "sub", style: "margin:0 0 6px",
        text: "Needs first: " + r.requires.map((id) => (byId[id] && byId[id].title) || id).join(", ") }));
    }
    const actions = el("div", { style: "display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap" });
    if (r.status === "in_progress" && r.job_id) {
      actions.append(el("a", { class: "btn btn-primary", href: `#/job/${r.job_id}`, text: "Continue the walkthrough" }));
    } else if (r.status === "on") {
      actions.append(el("span", { class: "sub", text: "Nothing to do." }));
      if (r.job_id) actions.append(el("a", { class: "btn btn-ghost", href: `#/job/${r.job_id}`, text: "See how it was set up" }));
    } else {
      const btn = el("button", { class: "btn btn-primary", disabled: r.status === "blocked" || undefined,
        text: "Walk me through it" + (r.effort ? ` · ${r.effort}` : "") });
      btn.addEventListener("click", () => start(r, btn));
      actions.append(btn);
      if (r.job_id) actions.append(el("a", { class: "btn btn-ghost", href: `#/job/${r.job_id}`,
        text: `Last attempt (${(r.job_status || "unknown").replace(/_/g, " ")})` }));
    }
    parts.push(actions);
    return el("div", { class: "card card-pad" }, ...parts);
  }

  async function start(r, btn) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Opening the walkthrough…";
    try {
      const res = await api.post(`/setup/recipes/${r.id}/start`);
      toast("Walkthrough opened — the engine is reading the brief. Approve it with Assist on when it is ready.", "ok");
      // Same landing as a submitted idea: the job hub's Overview embeds the
      // approval gate while the brief refines.
      router.navigate(res && res.job_id ? `/job/${res.job_id}` : "/capabilities");
    } catch (e) {
      btn.disabled = false;
      btn.textContent = label;
      toast((e && e.message) || "Could not open the walkthrough", "err");
    }
  }

  load();
  return () => { disposed = true; };
}
