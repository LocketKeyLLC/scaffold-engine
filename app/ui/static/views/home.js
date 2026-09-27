// Home — your jobs as NEXT ACTIONS. Replaces the Dashboard (stat tiles, status
// strip, quick-action buttons, services, model roles, recent-jobs table), the
// Jobs list, the Assistant session picker and the Approvals list: one list,
// each row naming the job, where it is in plain words, and the ONE button that
// matters for it right now. Health and model roles moved to Settings › Status.
import * as api from "../api.js";
import { el, mount, timeAgo } from "../util.js";
import { storage } from "../storage.js";
import { statusBadge, loading, errorPanel, nextActionChips, makeClickable, assistSessionFromActions } from "../components.js";
import { bucketOf, nextStep } from "../vocab.js";

// First-run onboarding: shown once when the install has zero jobs, then
// suppressed via this localStorage flag (survives navigation + reloads).
const ONBOARD_KEY = "scaffold_onboarded";
const ACCOUNT_PROMPT_KEY = "scaffold_account_prompt_dismissed";

// Filter chips. "Active" is the default: everything that is not history.
export const FILTERS = [
  ["active", "Active"],
  ["needs_you", "Needs you"],
  ["done", "Done"],
  ["all", "All"],
];
// Old #/jobs/:filter addresses (dashboard tiles, palette, chat links) keep working.
export const FILTER_ALIASES = {
  attention: "needs_you", awaiting_confirmation: "needs_you", running: "active",
  completed: "done", terminal: "done", cancelled: "done", failed: "needs_you",
};
export function resolveFilter(raw) {
  const f = FILTER_ALIASES[raw] || raw || "active";
  return FILTERS.some(([k]) => k === f) ? f : "active";
}

export function matchesFilter(job, filter) {
  const b = bucketOf(job.status);
  if (filter === "all") return true;
  if (filter === "active") return b !== "done";
  return b === filter;
}

/** Pure: what a row shows — the plain stage, the hint, the one action. */
export function rowModel(job, { work = null, progress = null } = {}) {
  const step = nextStep(job, { progress });
  const sid = work ? assistSessionFromActions(work.next_actions) : null;
  const when = job.completed_at || job.updated_at || job.created_at;
  return {
    id: job.id,
    title: job.title || "(untitled)",
    status: job.status,
    bucket: bucketOf(job.status),
    hint: step.hint,
    action: step,
    sessionId: sid,
    when,
    part: job.parent_job_id ? `part ${job.component_index != null ? job.component_index + 1 : "?"} of a larger build` : "",
    nodes: job.node_count || 0,
  };
}

export default function home(container, params) {
  let filter = resolveFilter(params && params.filter);
  let query = "";
  let jobs = [];
  let workById = new Map();
  let progressById = new Map();
  let health = null;
  let account = null;
  let disposed = false;
  let timer = null;
  let tickN = 0;

  const search = el("input", {
    class: "input input-sm home-search",
    placeholder: "Find a job…",
    "aria-label": "Find a job",
    onInput: (e) => { query = e.target.value.trim().toLowerCase(); renderList(); },
  });
  const listEl = el("div", { class: "home-list" }, loading("Loading your jobs…"));
  const chipsEl = el("div", { class: "row row-wrap home-chips" });
  const noticeEl = el("div", { class: "home-notices" });

  mount(
    container,
    el("div", { class: "view-header home-head" },
      el("div", {}, el("h1", { text: "Home" })),
      el("div", { class: "header-actions" }, el("a", { class: "btn btn-primary", href: "#/new", text: "＋ New idea" }))),
    noticeEl,
    el("div", { class: "row row-wrap home-toolbar" }, chipsEl, el("span", { class: "spacer" }), search),
    listEl
  );

  function chips() {
    const count = (f) => jobs.filter((j) => matchesFilter(j, f)).length;
    mount(chipsEl, ...FILTERS.map(([f, label]) =>
      el("button", {
        class: "btn btn-sm home-chip" + (f === filter ? " btn-primary" : ""),
        text: `${label} ${count(f)}`,
        "aria-pressed": f === filter ? "true" : "false",
        onClick: () => {
          filter = f;
          history.replaceState(null, "", f === "active" ? "#/" : `#/jobs/${f}`);
          chips();
          renderList();
        },
      })));
  }

  // §17.840 — used installs never see the wizard automatically, so this is
  // the one place an existing operator discovers the admin-account step.
  function accountPrompt() {
    if (!account || account.claimed) return null;
    if (api.principal()?.is_admin === false) return null;
    if (storage.get(ACCOUNT_PROMPT_KEY)) return null;
    return el("div", { class: "card card-pad home-notice" },
      el("span", { text: "Create your admin account — sign in with a name and password instead of the API key. " }),
      el("a", { class: "btn btn-sm btn-primary", href: "#/setup", text: "Set it up" }),
      el("button", { class: "btn btn-ghost btn-sm", text: "Dismiss", onClick: (e) => {
        storage.set(ACCOUNT_PROMPT_KEY, "1");
        e.target.closest(".home-notice")?.remove();
      } }));
  }

  // Health warnings (unpulled role models, redis down, …) — one quiet line.
  function setupNotice() {
    const warns = health?.warnings || [];
    if (!warns.length) return null;
    return el("div", { class: "card card-pad home-notice warn" },
      el("span", { text: `${warns.length} setup item${warns.length === 1 ? "" : "s"} need attention — ${warns[0]}${warns.length > 1 ? " …" : ""}` }),
      el("a", { class: "btn btn-sm", href: "#/settings/status", text: "See status" }));
  }

  // First-run welcome: a 3-step orientation shown only on an empty install.
  function welcomeCard() {
    const step = (n, title, body) =>
      el("div", { class: "welcome-step" },
        el("div", { class: "welcome-step-n", text: String(n) }),
        el("div", {}, el("div", { class: "welcome-step-t", text: title }), el("div", { class: "welcome-step-b dim", text: body })));
    return el("div", { class: "card card-pad welcome-card" },
      el("img", { class: "welcome-logo", src: "/ui/static/logo.svg", alt: "" }),
      el("h2", { class: "welcome-title", text: "Welcome to Scaffold Engine" }),
      el("p", { class: "welcome-sub dim", text: "Describe what you want built; the engine turns it into a plan you approve, then walks you through it or runs it." }),
      el("div", { class: "welcome-steps" },
        step(1, "Describe an idea", "In plain words — hardware, constraints, what you already have, what you want at the end."),
        step(2, "Approve the plan", "The engine refines your idea, asks what it needs to know, researches, and draws the plan."),
        step(3, "Walk through it", "One step at a time, with exact commands for your setup — or let the engine run the plan itself.")),
      el("div", { class: "welcome-actions row" },
        el("a", { class: "btn btn-primary", href: "#/new", text: "＋ Describe your first idea" }),
        el("button", { class: "btn btn-ghost btn-sm", text: "Dismiss", onClick: () => { storage.set(ONBOARD_KEY, "1"); renderList(); } })));
  }

  function row(job) {
    const m = rowModel(job, { work: workById.get(job.id), progress: progressById.get(job.id) });
    const actionBtn = el("a", {
      class: "btn btn-sm home-action" + (m.action.primary ? " btn-primary" : ""),
      href: m.action.href, text: m.action.label + " →",
    });
    // §17.1134 — the server's next_actions as quiet chips (only /work rows carry them)
    const w = workById.get(job.id);
    const chipsRow = w ? nextActionChips(w.next_actions, { jobId: job.id, limit: 2, kinds: ["call"], onDone: () => load() }) : null;
    const meta = el("div", { class: "row row-wrap home-row-meta" },
      statusBadge(job.status),
      m.hint ? el("span", { class: "dim home-row-hint", text: m.hint }) : null,
      m.part ? el("span", { class: "faint", text: `· ${m.part}` }) : null,
      el("span", { class: "faint", text: `· ${m.bucket === "done" && job.completed_at ? "finished" : "updated"} ${timeAgo(m.when)}` }));
    const card = el("div", { class: `card home-row bucket-${m.bucket}` },
      el("div", { class: "home-row-main" },
        el("div", { class: "home-row-title", text: m.title }),
        meta),
      el("div", { class: "home-row-actions" }, chipsRow, actionBtn));
    // The whole row goes where the button goes (keyboard-reachable, §17.854 G6);
    // the chips inside keep their own handlers.
    makeClickable(card, (e) => {
      if (e.target.closest("a, button")) return;
      location.hash = m.action.href;
    }, { role: "link", label: `${m.action.label}: ${m.title}` });
    return card;
  }

  function renderList() {
    if (!jobs.length && !storage.get(ONBOARD_KEY)) {
      mount(listEl, welcomeCard());
      return;
    }
    const visible = jobs.filter((j) => matchesFilter(j, filter) && (!query || (j.title || "").toLowerCase().includes(query)));
    if (!visible.length) {
      mount(listEl, el("div", { class: "card empty-state small" },
        el("p", { text: query ? "No jobs match that search." : filter === "needs_you" ? "Nothing needs you right now." : filter === "done" ? "Nothing finished yet." : "No active jobs." }),
        !query && filter !== "all" && jobs.length ? el("button", { class: "btn btn-sm btn-ghost", text: "Show all jobs", onClick: () => { filter = "all"; chips(); renderList(); } }) : null,
        !jobs.length ? el("a", { class: "btn btn-primary", href: "#/new", text: "＋ New idea" }) : null));
      return;
    }
    mount(listEl, ...visible.map(row));
  }

  // §17.818 / §17.1118 — one batched /exec/statuses call fills the step
  // positions of every job that is being worked through.
  async function fillProgress() {
    const ids = jobs.filter((j) => ["executing", "running", "assisted_executing", "assisted_running", "assisted_paused"].includes(j.status)).map((j) => j.id);
    if (!ids.length) { progressById = new Map(); return; }
    try {
      const res = await api.get("/exec/statuses", { query: { ids: ids.join(",") } });
      const statuses = (res && res.statuses) || {};
      progressById = new Map(ids.map((id) => [id, statuses[id] && statuses[id].progress]).filter(([, p]) => p && p.total != null));
    } catch (e) {
      console.debug("home: /exec/statuses unavailable — rows show no step position", e);
    }
  }

  async function load() {
    if (disposed) return;
    try {
      const refreshEnrichment = tickN % 6 === 0;   // §17.1118 — slow-changing reads every 60 s
      tickN += 1;
      const pages = [];
      let offset = 0;
      for (;;) {   // /jobs caps limit at 100 — page through, bounded at 500 rows
        const res = await api.get(`/jobs?limit=100&offset=${offset}`);
        if (disposed) return;
        const page = res.jobs || [];
        pages.push(...page);
        offset += page.length;
        if (page.length < 100 || offset >= Math.min(res.total ?? offset, 500)) break;
      }
      const [work, h, acct] = await Promise.all([
        api.get("/work").catch(() => ({ jobs: [] })),
        refreshEnrichment ? api.health().catch(() => null) : Promise.resolve(health),
        refreshEnrichment ? api.accountStatus() : Promise.resolve(account),
      ]);
      if (disposed) return;
      jobs = pages;
      workById = new Map((work.jobs || []).map((j) => [j.id, j]));
      health = h; account = acct;
      await fillProgress();
      if (disposed) return;
      mount(noticeEl, accountPrompt(), setupNotice());
      chips();
      renderList();
    } catch (e) {
      if (!disposed) mount(listEl, errorPanel(e, () => load()));
    }
  }

  load();
  // §17.818 — don't poll a hidden tab.
  timer = setInterval(() => { if (!document.hidden) load(); }, 10000);

  return () => {
    disposed = true;
    if (timer) clearInterval(timer);
  };
}
