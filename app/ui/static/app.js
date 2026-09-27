// Scaffold Engine operator SPA — bootstrap, chrome, auth gate, view lifecycle.
import { el, mount, currentJob } from "./util.js";
import * as api from "./api.js";
import * as router from "./router.js";
import { INITIAL as HEALTH_INITIAL, nextHealth, healthText as healthLabel, dotState, pillVisible } from "./health_state.js";
import { placeholder } from "./views/placeholder.js";
import { mountCommandPalette } from "./command_palette.js";
import { toast } from "./components.js";
import { NAV, placeOf } from "./nav.js";
import * as notify from "./notify.js";
import { storage } from "./storage.js";
import { UI_BUILD, THEME_LABELS, THEME_NAMES, currentTheme, cycleTheme } from "./prefs.js";

// §17.1055 — the rail retracts on wide screens (the operator asked for the
// room: a walkthrough plus a terminal side by side). Per-browser, like theme
// and density; the ≤820px bottom bar is unaffected.
const SIDEBAR_COLLAPSED_KEY = "scaffold_sidebar_collapsed";
function sidebarCollapsed() {
  try { return storage.get(SIDEBAR_COLLAPSED_KEY) === "1"; } catch { return false; }
}

// ── Global error surface ──────────────────────────────────────────────
// A backstop so anything that escapes a view's own try/catch becomes a
// visible toast instead of vanishing into the console. Deduped (a render
// loop can't spam) and quiet for benign fetch-abort-on-navigation.
let _lastErr = { msg: "", at: 0 };
function surfaceError(err) {
  if (err && err.name === "AbortError") return;
  const raw = err?.detail || err?.message || (typeof err === "string" ? err : "Unexpected error");
  const msg = String(raw);
  const now = Date.now();
  if (msg === _lastErr.msg && now - _lastErr.at < 4000) return;
  _lastErr = { msg, at: now };
  // §17.846 — error toasts are now sticky (components.js), and the backstop
  // points at the console so the full stack is always reachable.
  const shown = msg.length > 160 ? msg.slice(0, 157) + "…" : msg;
  toast(`${shown} — details in the browser console (F12)`, "err");
}
window.addEventListener("unhandledrejection", (e) => surfaceError(e.reason));
window.addEventListener("error", (e) => { if (e.error) surfaceError(e.error); });

// §17.815 — a 401 mid-session (rotated/revoked key) sends the operator back to
// the connect gate. No-op while the gate is already showing (the gate's own
// validation 401 must not loop it).
window.addEventListener("scaffold:unauthorized", () => {
  if (!document.querySelector(".shell")) return;
  api.setKey("");
  connectGate("Your key was rejected (401) — it may have been rotated. Re-enter it.");
});

// §17.847 — click-to-copy on every markdown code block. Delegated (blocks are
// injected via innerHTML under a strict CSP — no inline handlers). Copies the
// CODE only: the language label and the button live outside <code>.
document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".md-copy");
  if (!btn) return;
  const code = btn.closest(".md-pre")?.querySelector("code");
  if (!code) return;
  try {
    // §17.877 — strip the fence's trailing newline: copying a command WITH a
    // trailing \n makes a terminal EXECUTE it on paste (operator report — every
    // pasted command "pressed enter by itself"). The operator should always be
    // the one who runs the command.
    let text = code.textContent.replace(/\s+$/, "");
    // §17.1159 — a walkthrough block copied for a step gets the step SENTINEL
    // appended: the paste then names the step and the exact block it came
    // from, and its presence at the end proves the block ran to completion.
    // Only shell-shaped blocks under a step bubble (data-step on the bubble).
    const stepEl = btn.closest("[data-step]");
    const lang = btn.closest(".md-pre")?.querySelector(".md-lang")?.textContent || "";
    if (stepEl?.dataset.step && (!lang || /^(bash|sh|shell)$/i.test(lang)) && !/== S:/.test(text)) {
      const { sentinelFor } = await import("./util.js");
      text = text + "\n" + sentinelFor(stepEl.dataset.step, text);
    }
    await navigator.clipboard.writeText(text);
    const old = btn.textContent;
    btn.textContent = "✓ copied";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1400);
  } catch {
    toast("Copy failed — select the text manually.", "err");
  }
});

const root = document.getElementById("root");
let outlet = null; // the content container the active view renders into
let cleanup = () => {}; // teardown hook returned by the active view
// §17.854 (audit G5) — chrome is rebuilt on every gate→boot cycle (mid-session
// 401 → connectGate → boot()). These module-scope handles let each rebuild
// REPLACE rather than ACCUMULATE its health poller + document keydown listener,
// which previously leaked one interval (fetching /health forever against a
// detached DOM) and one duplicate Escape handler per re-auth.
let healthTimer = null;
let attentionTimer = null; // §17.1007 — replaced, not stacked, per chrome rebuild
let sidebarKeyHandler = null;  // §17.1055
let jobPinHandler = null; // §17.896 — replaced, not stacked, per chrome rebuild

// ── Auth / connect gate ───────────────────────────────────────────────
function gateStep(n, title, ...body) {
  return el(
    "div",
    { class: "gate-step" },
    el("div", { class: "gate-step-n", text: String(n) }),
    el(
      "div",
      {},
      el("div", { class: "gate-step-t", text: title }),
      el("div", { class: "gate-step-b" }, ...body)
    )
  );
}

// §17.840 — password sign-in, shown when an admin account exists. The key
// gate stays one click away ("Use an API key instead") for recovery.
function passwordGate(displayName, message) {
  const input = el("input", {
    type: "password",
    class: "input",
    placeholder: "Password",
    autocomplete: "current-password",
  });
  const status = el("div", { class: "gate-status" }, message || "");
  const btn = el("button", { class: "btn btn-primary", text: "Sign in" });

  async function submit() {
    if (!input.value) {
      status.textContent = "Enter your password.";
      return;
    }
    btn.disabled = true;
    status.textContent = "Signing in…";
    try {
      await api.login(input.value);
      boot();
    } catch (e) {
      status.textContent = e.detail || e.message;
      btn.disabled = false;
    }
  }

  btn.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });

  mount(
    root,
    el(
      "div",
      { class: "gate" },
      el(
        "div",
        { class: "gate-card card" },
        el("img", { class: "gate-logo", src: "/ui/static/logo.svg", alt: "" }),
        el("h1", { class: "gate-title", text: "Scaffold Engine" }),
        el("p", { class: "gate-sub", text: `Welcome back, ${displayName}` }),
        input,
        btn,
        status,
        el("button", {
          class: "btn btn-ghost btn-sm gate-alt",
          text: "Use an API key instead",
          onClick: () => connectGate(),
        })
      )
    )
  );
  input.focus();
}

function connectGate(message) {
  const input = el("input", {
    type: "password",
    class: "input",
    placeholder: "X-API-Key",
    value: api.getKey(),
    autocomplete: "off",
  });
  const status = el("div", { class: "gate-status" }, message || "");
  const btn = el("button", { class: "btn btn-primary", text: "Connect" });

  async function submit() {
    const key = input.value.trim();
    if (!key) {
      status.textContent = "Enter your API key.";
      return;
    }
    api.setKey(key);
    btn.disabled = true;
    status.textContent = "Verifying…";
    try {
      const ok = await api.validateKey();
      if (ok) {
        boot();
      } else {
        api.setKey("");
        status.textContent = "Invalid API key (401). Check the value and retry.";
        btn.disabled = false;
      }
    } catch (e) {
      status.textContent = `Cannot reach orchestrator: ${e.message}`;
      btn.disabled = false;
    }
  }

  btn.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });

  mount(
    root,
    el(
      "div",
      { class: "gate" },
      el(
        "div",
        { class: "gate-card card" },
        el("img", { class: "gate-logo", src: "/ui/static/logo.svg", alt: "" }),
        el("h1", { class: "gate-title", text: "Scaffold Engine" }),
        el("p", { class: "gate-sub", text: "Operator sign-in" }),
        el(
          "div",
          { class: "gate-steps" },
          gateStep(
            1,
            "Get your sign-in link",
            "On the server, open a terminal in the folder you installed Scaffold Engine into and run:",
            el("code", { class: "gate-code", text: "make signin-link" }),
            "Click the link it prints — this browser signs in as administrator, and you won't be asked again."
          ),
          gateStep(
            2,
            "Or paste the key by hand",
            "It's the SCAFFOLD_API_KEY line inside the (hidden) .env file in that same folder. Paste the value below and press Connect — it stays in this browser only."
          ),
          gateStep(
            3,
            "Connect your models",
            "That happens after sign-in — the Setup wizard links the engine to your Ollama models (local or cloud). Then describe an idea and watch it run."
          )
        ),
        input,
        btn,
        status,
        el("p", {
          class: "gate-hint",
          text: "Sent as the X-API-Key header on each request.",
        })
      )
    )
  );
  input.focus();
}

// ── App chrome: the rail + content outlet ─────────────────────────────
// UX overhaul (2026-09-27). The 220 px sidebar (5 groups, 19 destinations, a
// pinned job block, an execution-mode block and a six-toggle footer) is a
// 64 px rail: three places, the current job, the theme, and a small menu. On
// phones the same rail is a bottom bar. Preferences live in Settings.
function railLink(n, active) {
  return el("a", { class: "rail-link" + (active ? " active" : ""), href: "#" + n.path, dataset: { nav: n.id }, title: n.label, "aria-label": n.label },
    el("span", { class: "rail-icon", text: n.icon }),
    el("span", { class: "rail-label", text: n.label }));
}

// §17.896 — the job the operator last opened stays one click away from every
// screen. One rail item, not a block: the job page's stage strip owns the
// rest (plan · run · output).
function renderJobLink(host) {
  const job = currentJob();
  if (!job) { host.hidden = true; mount(host); return; }
  host.hidden = false;
  const path = router.currentPath() || "/";
  mount(host, el("a", {
    class: "rail-link rail-job" + (path.startsWith(`/job/${job.id}`) ? " active" : ""),
    href: `#/job/${job.id}`, title: job.title || "Current job", "aria-label": `Current job: ${job.title || ""}`,
  }, el("span", { class: "rail-icon", text: "◎" }), el("span", { class: "rail-label", text: "Current" })));
}

function buildChrome() {
  outlet = el("main", { class: "content", id: "outlet" });

  const healthDot = el("span", { class: "health-dot", dataset: { state: "unknown" } });
  const healthText = el("span", { class: "health-text", text: "checking…" });
  const p = api.principal();

  const active = placeOf(router.currentPath() || "/");
  const jobLink = el("div", { class: "rail-job-slot" });
  renderJobLink(jobLink);
  if (jobPinHandler) window.removeEventListener("scaffold:currentjob", jobPinHandler);
  jobPinHandler = () => renderJobLink(jobLink);
  window.addEventListener("scaffold:currentjob", jobPinHandler);

  const navEl = el("nav", { class: "rail-nav", "aria-label": "Places" },
    ...NAV.map((n) => railLink(n, n.id === active)),
    jobLink);

  // Theme: one button, cycles auto → dark → light (the operator asked to keep
  // the switch). Everything else in the old footer is Settings › Preferences.
  const themeBtn = el("button", { class: "rail-btn", title: `Theme: ${THEME_NAMES[currentTheme()]}`, "aria-label": "Theme", text: THEME_LABELS[currentTheme()] });
  themeBtn.addEventListener("click", () => {
    const next = cycleTheme();
    themeBtn.textContent = THEME_LABELS[next];
    themeBtn.title = `Theme: ${THEME_NAMES[next]}`;
  });

  // The menu: who you are, engine health, the build, sign out.
  const menu = el("details", { class: "rail-menu" },
    el("summary", { class: "rail-btn", title: "Account, engine status, sign out", "aria-label": "Account and status" }, healthDot),
    el("div", { class: "rail-menu-body" },
      p ? el("div", { class: "identity", title: `key_id: ${p.key_id ?? "master"}` },
            el("span", { class: "identity-name", text: p.identity }),
            el("span", { class: "identity-role", text: ` (${p.role})` })) : null,
      el("div", { class: "health" }, healthText),
      el("div", { class: "faint mono ui-build", text: `ui ${UI_BUILD}` }),
      el("a", { class: "btn btn-ghost btn-sm", href: "#/settings/preferences", text: "Preferences" }),
      el("button", { class: "btn btn-ghost btn-sm", text: "Sign out", onClick: () => { api.setKey(""); location.reload(); } })));
  const onDocClick = (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; };
  document.addEventListener("click", onDocClick);

  const collapseBtn = el("button", {
    class: "rail-btn rail-collapse",
    title: "Hide the rail (⌃\\ toggles)",
    "aria-label": "Hide the rail",
    text: "⟨",
    onClick: () => setSidebarCollapsed(true),
  });

  const rail = el("aside", { class: "rail" },
    el("a", { class: "rail-brand", href: "#/", title: "Scaffold Engine", "aria-label": "Scaffold Engine — Home" },
      el("img", { class: "brand-logo", src: "/ui/static/logo.svg", alt: "" })),
    navEl,
    el("div", { class: "rail-foot" }, themeBtn, menu, collapseBtn));

  // §17.1055 — the one control that survives a collapsed rail.
  const railBtn = el("button", {
    class: "sidebar-rail",
    title: "Show the rail (⌃\\ toggles)",
    "aria-label": "Show the rail",
    text: "☰",
    onClick: () => setSidebarCollapsed(false),
  });
  // §17.1116 — the connection pill lives on the shell, not in the rail.
  const connPill = el("div", { class: "conn-pill", role: "status", "aria-live": "polite", hidden: true });
  const shell = el("div", { class: "shell" + (sidebarCollapsed() ? " sidebar-collapsed" : "") }, rail, railBtn, outlet, connPill);
  function setSidebarCollapsed(on) {
    shell.classList.toggle("sidebar-collapsed", !!on);
    try { on ? storage.set(SIDEBAR_COLLAPSED_KEY, "1") : storage.remove(SIDEBAR_COLLAPSED_KEY); } catch { /* private mode */ }
    window.dispatchEvent(new CustomEvent("scaffold:sidebar", { detail: { collapsed: !!on } }));
  }
  if (sidebarKeyHandler) document.removeEventListener("keydown", sidebarKeyHandler);
  sidebarKeyHandler = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "\\") { e.preventDefault(); setSidebarCollapsed(!shell.classList.contains("sidebar-collapsed")); }
  };
  document.addEventListener("keydown", sidebarKeyHandler);
  mount(root, shell);
  mountCommandPalette(); // idempotent; overlay lives on document.body
  startHealthPolling(healthDot, healthText, connPill);
  startAttentionPolling(); // §17.1007
}

// §17.840 — set by the wizard's "Skip for now" AND the Home notice's Dismiss
// (home.js uses the same literal): stops the boot-time route into setup for
// operators who deliberately declined the account.
const ACCOUNT_PROMPT_KEY = "scaffold_account_prompt_dismissed";

// §17.1007 — the global attention watcher.
//
// Deliberately does NOT skip hidden tabs, which inverts the §17.818 rule that
// governs every other poller here. That rule exists to stop a backgrounded tab
// burning cycles rendering things nobody is looking at; this loop exists
// PRECISELY for the operator who is looking elsewhere, and skipping it while
// hidden would disable the one feature it provides. The 30s cadence (vs the
// Home page's 10s) is the concession.
const ATTENTION_LABEL = {
  awaiting_confirmation: "plan ready to approve",
  awaiting_assist: "waiting on you",
  awaiting_decision: "needs your decision",
  assisted_paused: "walkthrough paused",
  blocked: "blocked",
  failed: "run failed",
  completed: "run finished",
};

function startAttentionPolling() {
  // jobId -> status as of the previous tick. The FIRST tick only seeds this:
  // without that, every reload would announce every already-finished job in
  // the recent list as though it had just happened.
  let seen = null;

  async function tick() {
    let jobs;
    try {
      const st = await api.status();   // §17.1122 — shared, memoized
      jobs = st.recent_jobs || [];
    } catch {
      return; // transient — the health dot already reports reachability
    }
    const now = new Map(jobs.map((j) => [j.id, j.status]));
    if (seen === null) {
      seen = now;
      return;
    }
    let announced = 0;
    let lastLabel = "";
    for (const [id, status] of now) {
      const before = seen.get(id);
      const label = ATTENTION_LABEL[status];
      // Only a real transition INTO an attention state counts. An unchanged
      // status is the poller re-observing what it already reported.
      if (!label || before === undefined || before === status) continue;
      const job = jobs.find((j) => j.id === id);
      const title = (job && job.title) || "Untitled job";
      const fired = notify.announce({
        key: `${id}:${status}`,
        count: announced + 1,
        label,
        title: `${title} — ${label}`,
        body:
          status === "awaiting_confirmation"
            ? "The engine refined your idea and has questions. Open the job to review and approve."
            : status === "completed"
            ? "The run finished. The output is ready."
            : status === "failed"
            ? "The run stopped. The job's Run stage has the reason and the recovery verbs."
            : "This job is parked and needs you to continue it.",
        href: `#/job/${id}`,
      });
      if (fired) {
        announced += 1;
        lastLabel = label;
      }
    }
    if (announced) notify.setBadge(announced, lastLabel);
    seen = now;
  }

  tick();
  if (attentionTimer) clearInterval(attentionTimer);
  attentionTimer = setInterval(tick, 30000);
}

function highlightNav(path) {
  const active = placeOf(path);
  document.querySelectorAll(".rail-link[data-nav]").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === active);
  });
  // §17.896 — keep the current-job link's highlight in step with the route.
  const slot = document.querySelector(".rail-job-slot");
  if (slot) renderJobLink(slot);
}

async function startHealthPolling(dot, text, pill) {
  // §17.1116 (ledger U-5) — a state with memory (health_state.js): the text
  // says "unreachable since 14:02 · last seen 3 min ago", and the fixed pill
  // (outside the rail, so it survives the §17.1055 collapsed layout) is
  // shown whenever the engine is not simply up.
  let state = HEALTH_INITIAL;
  function render() {
    dot.dataset.state = dotState(state);
    text.textContent = healthLabel(state);
    if (pill) {
      pill.dataset.state = state.state;
      pill.textContent = pillVisible(state) ? `⚠ ${healthLabel(state)}` : "";
      pill.hidden = !pillVisible(state);
    }
  }
  async function tick() {
    try {
      const h = await api.health();
      state = nextHealth(state, { ok: true, status: h && h.status });
    } catch (e) {
      state = nextHealth(state, { ok: false, error: e && (e.message || e.detail) });
    }
    render();
  }
  await tick();
  // §17.854 (audit G5) — clear any prior poller so a chrome rebuild doesn't
  // accumulate intervals hitting /health against detached nodes forever.
  if (healthTimer) clearInterval(healthTimer);
  healthTimer = setInterval(() => { if (!document.hidden) tick(); }, 15000); // §17.818 — skip hidden tabs
}

// ── View lifecycle ────────────────────────────────────────────────────
function renderView(viewFn, params, path) {
  try {
    cleanup();
  } catch {
    /* ignore */
  }
  cleanup = () => {};
  highlightNav(path);
  outlet.scrollTop = 0;
  const ret = viewFn(outlet, params);
  if (typeof ret === "function") cleanup = ret;
}

// Lazy view loader — each view is its own ES module, imported on first use.
// A failed import (parse/runtime error) must be LOUD, not silently swallowed
// into a placeholder — that once masked a real syntax error for a whole phase.
function lazy(name, title) {
  return () =>
    import(`./views/${name}.js`).catch((e) => {
      console.error(`[ui] failed to load view "${name}":`, e);
      return { default: placeholder(title, `Failed to load: ${e.message}`) };
    });
}
const VIEWS = {
  notfound: lazy("notfound", "Page not found"),
  home: lazy("home", "Home"),
  new: lazy("compose", "New idea"),
  chat: lazy("compose", "New idea"),   // the Chat page is folded into New idea (Ask the engine)
  // §17.859 — the job page owns a job's whole life (stages); its renderers
  // are imported by job_hub.js directly.
  job_hub: lazy("job_hub", "Job"),
  assist: lazy("assist", "Assistant"),
  compare: lazy("compare", "Compare Jobs"),
  knowledge: lazy("knowledge", "Knowledge"),
  settings: lazy("settings", "Settings"),
  setup: lazy("setup", "Connect your models"),
};

// §17.854 (audit G5) — monotonically-increasing nav token. Two quick
// navigations (first visit to each view, so the dynamic import actually hits the
// network) can resolve out of order; without a sequence check the earlier
// route's module could resolve LAST, tear down the newer view, and render the
// stale one while the hash points elsewhere. Each call stamps a token and bails
// if a newer navigation superseded it before its import resolved.
let navSeq = 0;
async function loadAndRender(name, params, path) {
  const token = ++navSeq;
  const mod = await VIEWS[name]();
  if (token !== navSeq) return; // a newer navigation won; drop this stale render
  renderView(mod.default, params, path);
}

// Old addresses keep resolving — as the tab of the place that now holds them.
// A dead link must never look like a navigation (§17.1114), so every retired
// route is REGISTERED here and lands on its successor with the same content.
function registerRoutes() {
  // Literal route calls on purpose: tests/test_spa_route_inventory.py scans
  // them to prove every in-SPA link resolves.
  const go = (name, p, fixed = {}) => loadAndRender(name, { ...p, ...fixed }, router.currentPath());
  router.route("/", (p) => go("home", p));
  router.route("/jobs", (p) => go("home", p));
  router.route("/jobs/:filter", (p) => go("home", p));
  router.route("/approvals", (p) => go("home", p, { filter: "needs_you" }));
  router.route("/assist", (p) => go("home", p, { filter: "active" }));
  router.route("/assist/:sessionId", (p) => go("assist", p));            // §17.1162 — forwards to the job's Run stage
  router.route("/new", (p) => go("new", p));
  router.route("/chat", (p) => go("chat", p));
  router.route("/job/:jobId", (p) => go("job_hub", p));
  router.route("/job/:jobId/:tab", (p) => go("job_hub", p));
  router.route("/compare", (p) => go("compare", p));
  router.route("/compare/:jobA", (p) => go("compare", p));
  router.route("/compare/:jobA/:jobB", (p) => go("compare", p));
  router.route("/knowledge", (p) => go("knowledge", p));
  router.route("/knowledge/:tab", (p) => go("knowledge", p));
  router.route("/knowledge/:tab/:sessionId", (p) => go("knowledge", p));
  router.route("/research", (p) => go("knowledge", p, { tab: "research" }));
  router.route("/research/:sessionId", (p) => go("knowledge", p, { tab: "research" }));
  router.route("/rag", (p) => go("knowledge", p, { tab: "search" }));
  router.route("/library", (p) => go("knowledge", p, { tab: "library" }));
  router.route("/schedules", (p) => go("knowledge", p, { tab: "schedules" }));
  router.route("/settings", (p) => go("settings", p));
  router.route("/settings/:tab", (p) => go("settings", p));
  router.route("/models", (p) => go("settings", p, { tab: "models" }));
  router.route("/capabilities", (p) => go("settings", p, { tab: "capabilities" }));
  router.route("/costs", (p) => go("settings", p, { tab: "costs" }));
  router.route("/traces", (p) => go("settings", p, { tab: "traces" }));
  router.route("/alerts", (p) => go("settings", p, { tab: "alerts" }));
  router.route("/setup", (p) => go("setup", p));
  // §17.1114 (ledger U-1) — an unknown route is a visible "Page not found",
  // never Home: a dead link must look like a dead link.
  router.setNotFound((path) => loadAndRender("notfound", { path }, path));
}

// ── Boot ──────────────────────────────────────────────────────────────
let started = false;
// Where does a fresh sign-in land?
// §17.817 — an empty engine routes its admin to the wizard once per INSTALL
// (server-side flag). §17.840 — beyond that, an admin who hasn't created
// their account (nor skipped it) goes to setup FIRST: the front door is
// user setup, not the console. Fail-soft: an older server or non-admin
// never redirects.
async function maybeFirstRun() {
  const p = api.principal();
  if (p?.is_admin === false) return;
  if (location.hash.startsWith("#/setup")) return;
  try {
    const fr = await api.firstRun();   // §17.1122
    if (fr && fr.first_run) {
      location.hash = "#/setup";
      return;
    }
  } catch {
    /* pre-§17.817 server */
  }
  const acct = await api.accountStatus();
  if (acct && !acct.claimed && !storage.get(ACCOUNT_PROMPT_KEY)) {
    location.hash = "#/setup";
  }
}
async function boot() {
  buildChrome();
  maybeFirstRun();
  if (!started) {
    registerRoutes();
    router.start();
    started = true;
  } else {
    // chrome was rebuilt (e.g. after gate); re-dispatch the current route
    // through the router so the same handler (with its fixed params) runs.
    router.redispatch();
  }
}

async function main() {
  // One-click pairing (§17.840): `make bootstrap` prints /ui/?key=<operator
  // key>. Adopt it, then immediately strip it from the address bar + history
  // so the secret doesn't linger on screen. Same pattern as Jupyter's token
  // links; the manual paste gate below remains the fallback.
  const urlKey = new URLSearchParams(location.search).get("key");
  if (urlKey && urlKey.trim()) {
    api.setKey(urlKey.trim());
    history.replaceState(null, "", location.pathname + location.hash);
  }
  if (!api.hasKey()) {
    // §17.840 — an install with an admin account gets the friendly password
    // gate; everything else (fresh install, pre-account, no-master multi-user)
    // gets the key gate with its step-by-step instructions.
    const acct = await api.accountStatus();
    if (acct?.claimed && acct?.login_available) passwordGate(acct.display_name);
    else connectGate();
    return;
  }
  // Have a key — verify it before showing the app.
  try {
    const ok = await api.validateKey();
    if (ok) boot();
    else connectGate("Stored key was rejected (401). Re-enter it.");
  } catch (e) {
    // Orchestrator unreachable — offer the gate with the error.
    connectGate(`Cannot reach orchestrator: ${e.message}`);
  }
}

main();
