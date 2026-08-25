// Scaffold Engine operator SPA — bootstrap, chrome, auth gate, view lifecycle.
import { el, mount } from "./util.js";
import * as api from "./api.js";
import * as router from "./router.js";
import { placeholder } from "./views/placeholder.js";
import { mountCommandPalette } from "./command_palette.js";
import { toast } from "./components.js";

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

const NAV = [
  { id: "new", path: "/new", label: "New idea", icon: "＋" },
  // §17.815 — native chat rides /v1, which is admin-only BY DESIGN (§17.810:
  // the loopback re-auths as master; scoped keys without identity-forwarding
  // would be an escalation). Hide it from non-admin identities; single-user
  // installs are always admin so nothing changes there.
  { id: "chat", path: "/chat", label: "Chat", icon: "💬", adminOnly: true },
  { id: "dashboard", path: "/", label: "Dashboard", icon: "◈" },
  { id: "approvals", path: "/approvals", label: "Approvals", icon: "⏻" },
  { id: "dag", path: "/dag", label: "DAG Canvas", icon: "⬡" },
  { id: "theater", path: "/theater", label: "Execution", icon: "▶" },
  { id: "output", path: "/output", label: "Outputs", icon: "▤" },
  { id: "compare", path: "/compare", label: "Compare", icon: "⇄" },
  { id: "research", path: "/research", label: "Research", icon: "◎" },
  { id: "assist", path: "/assist", label: "Assistant", icon: "✦" },
  // §17.816 — global model config is an admin surface (writes are
  // require_admin server-side).
  { id: "models", path: "/models", label: "Models", icon: "⚙" , adminOnly: true },
  { id: "rag", path: "/rag", label: "Knowledge", icon: "◉" },
  { id: "schedules", path: "/schedules", label: "Schedules", icon: "◷" },
  { id: "costs", path: "/costs", label: "Costs", icon: "◍" },
  { id: "traces", path: "/traces", label: "Traces", icon: "≣", adminOnly: true },
  { id: "alerts", path: "/alerts", label: "Alerts", icon: "⚑", adminOnly: true },
  { id: "settings", path: "/settings", label: "Settings", icon: "☰", adminOnly: true },
];

const root = document.getElementById("root");
let outlet = null; // the content container the active view renders into
let cleanup = () => {}; // teardown hook returned by the active view

// ── Auth / connect gate ───────────────────────────────────────────────
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
        el("div", { class: "gate-logo", text: "🧬" }),
        el("h1", { class: "gate-title", text: "Scaffold Engine" }),
        el("p", { class: "gate-sub", text: "Connect to the orchestrator" }),
        input,
        btn,
        status,
        el("p", {
          class: "gate-hint",
          text: "The key is stored in this browser only and sent as X-API-Key on each request.",
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
  const visibleNav = NAV.filter((n) => !n.adminOnly || p?.is_admin !== false);
  const navLinks = visibleNav.map((n) =>
    el(
      "a",
      { class: "nav-link", href: "#" + n.path, dataset: { nav: n.id } },
      el("span", { class: "nav-icon", text: n.icon }),
      el("span", { class: "nav-label", text: n.label })
    )
  );

  const healthDot = el("span", { class: "health-dot", dataset: { state: "unknown" } });
  const healthText = el("span", { class: "health-text", text: "checking…" });

  const sidebar = el(
    "aside",
    { class: "sidebar" },
    el(
      "div",
      { class: "brand" },
      el("span", { class: "brand-logo", text: "🧬" }),
      el("span", { class: "brand-name", text: "Scaffold" })
    ),
    el("nav", { class: "nav" }, ...navLinks),
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
      el("div", { class: "health" }, healthDot, healthText),
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
    el("span", { class: "brand-logo", text: "🧬" }),
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

function highlightNav(path) {
  const active = topSegment(path);
  document.querySelectorAll(".nav-link").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === active);
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
  setInterval(tick, 15000);
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
  router.route("/costs", (p) => loadAndRender("costs", p, router.currentPath()));
  router.route("/traces", (p) => loadAndRender("traces", p, router.currentPath()));
  router.route("/traces/:jobId", (p) => loadAndRender("traces", p, router.currentPath()));
  router.route("/alerts", (p) => loadAndRender("alerts", p, router.currentPath()));
  router.setNotFound(() => loadAndRender("dashboard", {}, "/"));
}

// ── Boot ──────────────────────────────────────────────────────────────
let started = false;
async function boot() {
  buildChrome();
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
  if (!api.hasKey()) {
    connectGate();
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
