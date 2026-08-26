// Command palette (Ctrl/Cmd-K). A global overlay for jumping between views and
// opening a job by search. Mounted once into document.body (idempotent — the
// chrome can rebuild across the connect gate). Injection-safe: every row is
// built with el() and job titles set via text, never innerHTML.
import * as api from "./api.js";
import * as router from "./router.js";
import { el, mount, shortId, debounce } from "./util.js";
import { NAV } from "./nav.js";

// Every view is jumpable — derived from the shared nav (this file used to
// keep its own copy, which drifted to 10 of 18 views). Admin-only entries
// are filtered at rebuild time against the cached principal (§17.815).
function navCommands() {
  const p = api.principal();
  return NAV.filter((n) => !n.adminOnly || p?.is_admin !== false);
}

// Subsequence fuzzy score; -1 if `q` is not a subsequence of `text`.
function fuzzy(q, text) {
  if (!q) return 0;
  q = q.toLowerCase();
  text = text.toLowerCase();
  let ti = 0,
    score = 0,
    streak = 0;
  for (const ch of q) {
    const idx = text.indexOf(ch, ti);
    if (idx === -1) return -1;
    streak = idx === ti ? streak + 1 : 0;
    score += 1 + streak;
    ti = idx + 1;
  }
  return score;
}

// Route a searched job to its most relevant view by status.
function routeForJob(j) {
  if (j.status === "awaiting_confirmation") return `/approvals/${j.id}`;
  if (j.status === "completed") return `/output/${j.id}`;
  return `/dag/${j.id}`;
}

let mounted = false;

export function mountCommandPalette() {
  if (mounted) return;
  mounted = true;

  let open = false;
  let curQuery = "";
  let jobItems = [];
  let items = [];
  let activeIdx = 0;

  const input = el("input", { class: "cmdk-input", type: "text", placeholder: "Jump to a view or search a job…", autocomplete: "off", spellcheck: "false" });
  const list = el("div", { class: "cmdk-list" });
  const panel = el("div", { class: "cmdk-panel" }, el("div", { class: "cmdk-input-wrap" }, el("span", { class: "cmdk-kbd", text: "⌘K" }), input), list);
  const overlay = el("div", { class: "cmdk-overlay hidden" }, panel);
  document.body.append(overlay);

  overlay.addEventListener("mousedown", (e) => {
    if (e.target === overlay) close();
  });
  input.addEventListener("input", () => {
    curQuery = input.value;
    jobItems = [];
    rebuild();
    if (curQuery.trim()) searchJobs(curQuery);
  });

  const searchJobs = debounce(async (q) => {
    if (q !== curQuery) return; // stale
    try {
      const res = await api.get("/jobs", { query: { q: q.trim(), limit: 8 } });
      if (q !== curQuery) return;
      jobItems = (res.jobs || []).map((j) => ({
        label: j.title || "(untitled)",
        hint: `${shortId(j.id)} · ${j.status}`,
        run: () => router.navigate(routeForJob(j)),
      }));
      rebuild();
    } catch {
      /* transient — leave static items */
    }
  }, 250);

  function rebuild() {
    const q = curQuery.trim();
    const statics = navCommands().map((c) => ({ label: c.label, hint: "view", run: () => router.navigate(c.path), score: fuzzy(q, c.label) }))
      .filter((c) => q === "" || c.score >= 0)
      .sort((a, b) => b.score - a.score);
    items = [...statics, ...jobItems];
    if (activeIdx >= items.length) activeIdx = Math.max(0, items.length - 1);
    renderList();
  }

  function renderList() {
    if (!items.length) {
      mount(list, el("div", { class: "cmdk-empty", text: "No matches." }));
      return;
    }
    mount(
      list,
      ...items.map((it, i) =>
        el(
          "div",
          {
            class: "cmdk-item" + (i === activeIdx ? " active" : ""),
            onMousedown: (e) => {
              e.preventDefault();
              select(i);
            },
            onMouseenter: () => {
              activeIdx = i;
              highlight();
            },
          },
          el("span", { class: "cmdk-item-label", text: it.label }),
          el("span", { class: "cmdk-item-hint", text: it.hint || "" })
        )
      )
    );
  }

  function highlight() {
    const rows = list.querySelectorAll(".cmdk-item");
    rows.forEach((r, i) => r.classList.toggle("active", i === activeIdx));
  }

  function select(i) {
    const it = items[i];
    if (!it) return;
    close();
    it.run();
  }

  function openPalette() {
    open = true;
    overlay.classList.remove("hidden");
    input.value = "";
    curQuery = "";
    jobItems = [];
    activeIdx = 0;
    rebuild();
    input.focus();
  }

  function close() {
    open = false;
    overlay.classList.add("hidden");
  }

  function toggle() {
    open ? close() : openPalette();
  }

  window.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      toggle();
      return;
    }
    if (!open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      activeIdx = Math.min(items.length - 1, activeIdx + 1);
      highlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      activeIdx = Math.max(0, activeIdx - 1);
      highlight();
    } else if (e.key === "Enter") {
      e.preventDefault();
      select(activeIdx);
    }
  });
}
