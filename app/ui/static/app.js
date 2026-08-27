// Scaffold Engine operator SPA — bootstrap, chrome, auth gate, view lifecycle.
import { el, mount } from "./util.js";
import * as api from "./api.js";
import * as router from "./router.js";
import { placeholder } from "./views/placeholder.js";
import { mountCommandPalette } from "./command_palette.js";
import { toast } from "./components.js";
import { NAV, NAV_GROUPS } from "./nav.js";

// Visible build stamp (sidebar foot). Bump per UI change round — it exists so
// "is my tab running the latest UI?" is answerable at a glance instead of by
// diffing pixels (the §17.840/§17.842 stale-module debugging sink).
const UI_BUILD = "r2";

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
  toast(msg.length > 160 ? msg.slice(0, 157) + "…" : msg, "err");
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

// Nav structure (groups + admin flags) lives in nav.js, shared with the
// command palette.

const root = document.getElementById("root");
let outlet = null; // the content container the active view renders into
let cleanup = () => {}; // teardown hook returned by the active view

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

// ── App chrome (sidebar + topbar + content outlet) ────────────────────
function buildChrome() {
  outlet = el("main", { class: "content", id: "outlet" });

  // §17.815 — admin surfaces disappear for non-admin identities. Unknown
  // principal (pre-§17.815 server) fails open to admin: the server still
  // enforces authz on every request; this is navigation hygiene.
  const p = api.principal();
  const navLinks = [];
  const navGroups = NAV_GROUPS.map((g) => {
    const items = g.items.filter((n) => !n.adminOnly || p?.is_admin !== false);
    if (!items.length) return null;
    const links = items.map((n) =>
      el(
        "a",
        { class: "nav-link", href: "#" + n.path, dataset: { nav: n.id } },
        el("span", { class: "nav-icon", text: n.icon }),
        el("span", { class: "nav-label", text: n.label })
      )
    );
    navLinks.push(...links);
    // Collapsible group (operator request: keep the sections, don't overwhelm
    // — collapsed by default, except the group that owns the active view).
    const open = navOpenGroups().has(g.label) || items.some((n) => n.id === topSegment(router.currentPath() || "/"));
    const group = el(
      "div",
      { class: "nav-group" + (open ? "" : " collapsed"), dataset: { group: g.label } },
      el(
        "button",
        {
          class: "nav-group-label nav-group-toggle",
          onClick: () => {
            const collapsed = group.classList.toggle("collapsed");
            const set = navOpenGroups();
            collapsed ? set.delete(g.label) : set.add(g.label);
            saveNavOpenGroups(set);
          },
        },
        el("span", { class: "nav-group-chevron", text: "▸" }),
        g.label
      ),
      ...links
    );
    return group;
  }).filter(Boolean);

  const healthDot = el("span", { class: "health-dot", dataset: { state: "unknown" } });
  const healthText = el("span", { class: "health-text", text: "checking…" });

  const sidebar = el(
    "aside",
    { class: "sidebar" },
    el(
      "div",
      { class: "brand" },
      el("img", { class: "brand-logo", src: "/ui/static/logo.svg", alt: "" }),
      el("span", { class: "brand-name", text: "Scaffold" })
    ),
    el("nav", { class: "nav" }, ...navGroups),
    el(
      "div",
      { class: "sidebar-foot" },
      // §17.815 — who this key is (from /auth/whoami), so shared boxes show
      // real attribution instead of an anonymous session.
      p
        ? el(
            "div",
            { class: "identity", title: `key_id: ${p.key_id ?? "master"}` },
            el("span", { class: "identity-name", text: p.identity }),
            el("span", { class: "identity-role", text: ` (${p.role})` })
          )
        : null,
      el("div", { class: "foot-controls" }, themeToggle(), densityToggle()),
      el("div", { class: "health" }, healthDot, healthText, el("span", { class: "faint mono ui-build", text: ` · ui ${UI_BUILD}` })),
      el("button", {
        class: "btn btn-ghost btn-sm",
        text: "Sign out",
        onClick: () => {
          api.setKey("");
          location.reload();
        },
      })
    )
  );

  // ── Mobile chrome: hamburger + off-canvas slide-over ────────────────
  // On wide screens the sidebar is a static grid column and these are
  // display:none (CSS). At ≤820px the sidebar becomes a fixed drawer that
  // this scrim/hamburger open and close.
  function openNav() {
    sidebar.classList.add("open");
    scrim.classList.remove("hidden");
    hamburger.setAttribute("aria-expanded", "true");
  }
  function closeNav() {
    sidebar.classList.remove("open");
    scrim.classList.add("hidden");
    hamburger.setAttribute("aria-expanded", "false");
  }
  const scrim = el("div", { class: "scrim hidden", onClick: closeNav });
  const hamburger = el("button", {
    class: "hamburger",
    "aria-label": "Open navigation",
    "aria-expanded": "false",
    text: "☰",
    onClick: openNav,
  });
  const topbar = el(
    "div",
    { class: "mobile-topbar" },
    hamburger,
    el("img", { class: "brand-logo", src: "/ui/static/logo.svg", alt: "" }),
    el("span", { class: "brand-name", text: "Scaffold" })
  );
  // Tapping a destination navigates → close the drawer; Escape closes too.
  navLinks.forEach((a) => a.addEventListener("click", closeNav));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeNav();
  });

  mount(root, el("div", { class: "shell" }, topbar, sidebar, scrim, outlet));
  mountCommandPalette(); // idempotent; overlay lives on document.body
  startHealthPolling(healthDot, healthText);
}

// ── Theme + density toggles ───────────────────────────────────────────
// Persisted per-browser; theme_boot.js re-applies both before first paint so
// there's no flash. Theme cycles auto → dark → light; density toggles
// comfortable ↔ compact (token overrides in app.css).
const THEME_KEY = "scaffold_theme";
const DENSITY_KEY = "scaffold_density";
// §17.840 — set by the wizard's "Skip for now" AND the dashboard card's
// Dismiss (dashboard.js uses the same literal): stops the boot-time route
// into setup for operators who deliberately declined the account.
const ACCOUNT_PROMPT_KEY = "scaffold_account_prompt_dismissed";
const THEME_LABELS = { auto: "◐ Auto", dark: "● Dark", light: "○ Light" };

function themeToggle() {
  const cur = () => localStorage.getItem(THEME_KEY) || "auto";
  const btn = el("button", {
    class: "btn btn-ghost",
    title: "Theme (auto follows the OS)",
    text: THEME_LABELS[cur()],
  });
  btn.addEventListener("click", () => {
    const order = ["auto", "dark", "light"];
    const next = order[(order.indexOf(cur()) + 1) % order.length];
    if (next === "auto") {
      localStorage.removeItem(THEME_KEY);
      delete document.documentElement.dataset.theme;
    } else {
      localStorage.setItem(THEME_KEY, next);
      document.documentElement.dataset.theme = next;
    }
    btn.textContent = THEME_LABELS[next];
  });
  return btn;
}

function densityToggle() {
  const compact = () => localStorage.getItem(DENSITY_KEY) === "compact";
  const label = () => (compact() ? "▦ Compact" : "▢ Cozy");
  const btn = el("button", {
    class: "btn btn-ghost",
    title: "Density — compact tightens paddings for more rows per screen",
    text: label(),
  });
  btn.addEventListener("click", () => {
    if (compact()) {
      localStorage.removeItem(DENSITY_KEY);
      delete document.documentElement.dataset.density;
    } else {
      localStorage.setItem(DENSITY_KEY, "compact");
      document.documentElement.dataset.density = "compact";
    }
    btn.textContent = label();
  });
  return btn;
}

// Collapsed-group persistence: the OPEN set survives reloads; no stored
// value means "everything collapsed" (the active view's group still
// auto-expands so the operator always sees where they are).
const NAV_OPEN_KEY = "scaffold_nav_open";
function navOpenGroups() {
  try { return new Set(JSON.parse(localStorage.getItem(NAV_OPEN_KEY)) || []); }
  catch { return new Set(); }
}
function saveNavOpenGroups(set) {
  localStorage.setItem(NAV_OPEN_KEY, JSON.stringify([...set]));
}

function highlightNav(path) {
  const active = topSegment(path);
  document.querySelectorAll(".nav-link").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === active);
    // Deep links / palette jumps into a collapsed group must reveal the
    // active item — expand (without persisting: a navigation isn't a
    // deliberate "keep this open" choice).
    if (a.dataset.nav === active) a.closest(".nav-group")?.classList.remove("collapsed");
  });
}

// Detail routes without their own nav item map onto the sidebar item that owns
// their flow, so the sidebar highlights sensibly while on them.
const NAV_ALIAS = { plan: "approvals" };

function topSegment(path) {
  const seg = path.split("/").filter(Boolean)[0];
  if (!seg) return "dashboard";
  if (NAV_ALIAS[seg]) return NAV_ALIAS[seg];
  return NAV.some((n) => n.id === seg) ? seg : "dashboard";
}

async function startHealthPolling(dot, text) {
  async function tick() {
    try {
      const h = await api.health();
      const up = h.status === "ok" || h.status === "healthy" || h.status === "up";
      dot.dataset.state = up ? "up" : "degraded";
      text.textContent = up ? "orchestrator up" : `status: ${h.status}`;
    } catch {
      dot.dataset.state = "down";
      text.textContent = "unreachable";
    }
  }
  await tick();
  setInterval(() => { if (!document.hidden) tick(); }, 15000); // §17.818 — skip hidden tabs
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
  new: lazy("compose", "New idea"),
  chat: lazy("chat", "Chat"),
  dashboard: lazy("dashboard", "Dashboard"),
  jobs: lazy("jobs", "Jobs"),
  approvals: lazy("approvals", "Approval Gate"),
  dag: lazy("dag", "DAG Canvas"),
  plan: lazy("plan", "Plan Editor"),
  theater: lazy("theater", "Execution Theater"),
  output: lazy("output", "Output"),
  compare: lazy("compare", "Compare Jobs"),
  research: lazy("research", "Research Explorer"),
  assist: lazy("assist", "Assistant"),
  models: lazy("models", "Models"),
  rag: lazy("rag", "Knowledge (RAG)"),
  schedules: lazy("schedules", "Schedules"),
  settings: lazy("settings", "Settings"),
  setup: lazy("setup", "Connect your models"),
  library: lazy("library", "Library"),
  costs: lazy("costs", "Costs"),
  traces: lazy("traces", "LLM Traces"),
  alerts: lazy("alerts", "Alerts"),
};

async function loadAndRender(name, params, path) {
  const mod = await VIEWS[name]();
  renderView(mod.default, params, path);
}

function registerRoutes() {
  router.route("/", (p) => loadAndRender("dashboard", p, router.currentPath()));
  router.route("/new", (p) => loadAndRender("new", p, router.currentPath()));
  router.route("/chat", (p) => loadAndRender("chat", p, router.currentPath()));
  router.route("/jobs", (p) => loadAndRender("jobs", p, router.currentPath()));
  router.route("/jobs/:filter", (p) => loadAndRender("jobs", p, router.currentPath()));
  router.route("/approvals", (p) => loadAndRender("approvals", p, router.currentPath()));
  router.route("/approvals/:jobId", (p) => loadAndRender("approvals", p, router.currentPath()));
  router.route("/dag", (p) => loadAndRender("dag", p, router.currentPath()));
  router.route("/dag/:jobId", (p) => loadAndRender("dag", p, router.currentPath()));
  router.route("/plan/:jobId", (p) => loadAndRender("plan", p, router.currentPath()));
  router.route("/theater", (p) => loadAndRender("theater", p, router.currentPath()));
  router.route("/theater/:jobId", (p) => loadAndRender("theater", p, router.currentPath()));
  router.route("/output", (p) => loadAndRender("output", p, router.currentPath()));
  router.route("/output/:jobId", (p) => loadAndRender("output", p, router.currentPath()));
  router.route("/compare", (p) => loadAndRender("compare", p, router.currentPath()));
  router.route("/compare/:jobA/:jobB", (p) => loadAndRender("compare", p, router.currentPath()));
  router.route("/compare/:jobA", (p) => loadAndRender("compare", p, router.currentPath()));
  router.route("/research", (p) => loadAndRender("research", p, router.currentPath()));
  router.route("/research/:sessionId", (p) => loadAndRender("research", p, router.currentPath()));
  router.route("/assist", (p) => loadAndRender("assist", p, router.currentPath()));
  router.route("/assist/:sessionId", (p) => loadAndRender("assist", p, router.currentPath()));
  router.route("/models", (p) => loadAndRender("models", p, router.currentPath()));
  router.route("/rag", (p) => loadAndRender("rag", p, router.currentPath()));
  router.route("/schedules", (p) => loadAndRender("schedules", p, router.currentPath()));
  router.route("/settings", (p) => loadAndRender("settings", p, router.currentPath()));
  router.route("/setup", (p) => loadAndRender("setup", p, router.currentPath()));
  router.route("/library", (p) => loadAndRender("library", p, router.currentPath()));
  router.route("/costs", (p) => loadAndRender("costs", p, router.currentPath()));
  router.route("/traces", (p) => loadAndRender("traces", p, router.currentPath()));
  router.route("/traces/:jobId", (p) => loadAndRender("traces", p, router.currentPath()));
  router.route("/alerts", (p) => loadAndRender("alerts", p, router.currentPath()));
  router.setNotFound(() => loadAndRender("dashboard", {}, "/"));
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
    const fr = await api.get("/meta/first-run");
    if (fr && fr.first_run) {
      location.hash = "#/setup";
      return;
    }
  } catch {
    /* pre-§17.817 server */
  }
  const acct = await api.accountStatus();
  if (acct && !acct.claimed && !localStorage.getItem(ACCOUNT_PROMPT_KEY)) {
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
    // chrome was rebuilt (e.g. after gate); re-dispatch current route
    const path = router.currentPath();
    const seg = topSegment(path);
    loadAndRender(seg, router.getCurrent()?.params || {}, path);
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
