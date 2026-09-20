// Reusable UI components shared across views.
import { el } from "./util.js";
import * as api from "./api.js";
import { filterRenderable, actionLabel, actionTarget } from "./next_actions.js";

/**
 * §17.854 (audit G6) — make a non-button element behave like a button for
 * KEYBOARD users. Click-only handlers on <tr>/<div> tiles left the app's
 * carefully-defined :focus-visible rings as dead code and the elements
 * unreachable without a mouse. Adds tabindex + role + Enter/Space activation.
 * `role` defaults to "button"; pass "link" for row-navigations.
 */
export function makeClickable(node, handler, { role = "button", label } = {}) {
  node.setAttribute("tabindex", "0");
  node.setAttribute("role", role);
  if (label) node.setAttribute("aria-label", label);
  node.addEventListener("click", handler);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      handler(e);
    }
  });
  return node;
}

/** Status pill. Adds `st-<status>` for the color mapping in app.css. */
export function statusBadge(status) {
  const s = status || "unknown";
  return el("span", { class: `badge st-${s}`, text: s.replace(/_/g, " ") });
}

/** A labelled stat tile. `opts.accent` sets a color class; `opts.onClick` makes it clickable. */
export function statTile(label, value, opts = {}) {
  const val = el("div", { class: "stat-val", text: String(value) });
  if (opts.accent) val.classList.add(`accent-${opts.accent}`);
  const tile = el(
    "div",
    {
      class: "card stat" + (opts.onClick ? " clickable" : ""),
      title: opts.title || "",
    },
    val,
    el("div", { class: "stat-label", text: label })
  );
  // §17.854 (audit G6) — keyboard-activatable when clickable.
  if (opts.onClick) makeClickable(tile, opts.onClick, { label });
  return tile;
}

/** Centered loading spinner block. */
export function loading(text = "Loading…") {
  return el("div", { class: "loading-block" }, el("span", { class: "spin" }), el("span", { class: "dim", text }));
}

/** Error panel. */
export function errorPanel(err, retry) {
  const detail = err?.detail || err?.message || String(err);
  const status = err?.status ? ` (HTTP ${err.status})` : "";
  return el(
    "div",
    { class: "card empty-state" },
    el("div", { class: "empty-icon", text: "⚠️" }),
    el("p", { text: `${detail}${status}` }),
    retry ? el("button", { class: "btn btn-sm", text: "Retry", onClick: retry }) : null
  );
}

/**
 * Full-card empty state with an optional call-to-action.
 * opts: { icon, title, body, small, action }.
 * action: { label, href?, onClick?, primary?, newTab? } — an <a> when href is
 * given (a CTA to e.g. #/new), otherwise a <button>. Defaults to primary.
 */
export function emptyState({ icon, title, body, small = false, action } = {}) {
  const kids = [];
  if (icon) kids.push(el("div", { class: "empty-icon", text: icon }));
  if (title) kids.push(el("div", { class: "empty-title", text: title }));
  if (body) kids.push(el("p", { class: "empty-body", text: body }));
  if (action) {
    const cls = "btn " + (action.primary === false ? "btn-ghost" : "btn-primary");
    kids.push(
      action.href
        ? el("a", {
            class: cls,
            href: action.href,
            text: action.label,
            ...(action.newTab ? { target: "_blank", rel: "noopener" } : {}),
          })
        : el("button", { class: cls, text: action.label, onClick: action.onClick })
    );
  }
  return el("div", { class: "card empty-state" + (small ? " small" : "") }, ...kids);
}

const TOAST_ICON = { err: "⚠", ok: "✓", "": "ℹ" };
const TOAST_MAX = 4; // cap the stack so a burst can't cover the screen
let toastHost = null;

/**
 * Transient notification. `kind`: "" (info) | "ok" | "err".
 * `opts.duration` ms overrides the default TTL (errors persist longer);
 * pass 0 to require manual dismissal. Returns a dismiss() fn.
 * Each toast is its own live region (role=alert for errors → assertive,
 * role=status otherwise → polite) so screen readers announce it on insert.
 */
export function toast(msg, kind = "", opts = {}) {
  if (!toastHost) {
    toastHost = el("div", { class: "toast-host" });
    document.body.append(toastHost);
  }
  while (toastHost.children.length >= TOAST_MAX) toastHost.firstChild.remove();

  let dismissed = false;
  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 220);
  };
  const t = el(
    "div",
    { class: `toast ${kind}`.trim(), role: kind === "err" ? "alert" : "status" },
    el("span", { class: "toast-ico", "aria-hidden": "true", text: TOAST_ICON[kind] ?? TOAST_ICON[""] }),
    el("span", { class: "toast-msg", text: msg }),
    el("button", { class: "toast-x", text: "✕", title: "Dismiss", "aria-label": "Dismiss", onClick: dismiss })
  );
  toastHost.append(t);

  // §17.846 — errors are STICKY: they stay until the operator dismisses them.
  // A 6s auto-dismiss let a mid-approve failure vanish before it could be
  // read ("received an error that disappeared"). Success/info stay timed;
  // an explicit opts.duration still wins for callers that know better.
  const ttl = opts.duration ?? (kind === "err" ? 0 : 3200);
  if (ttl > 0) setTimeout(dismiss, ttl);
  return dismiss;
}

/** §17.1134 (ledger D-6) — render a job's `next_actions` as chips: navigation
 *  to the hub tab where the verb lives, or the id-in-path call the registry
 *  spells out (destructive ones confirm first). Noise ("wait") is filtered. */
export function nextActionChips(actions, { jobId, limit = 3, onDone } = {}) {
  const list = filterRenderable(actions).slice(0, limit);
  if (!list.length) return null;
  return el("div", { class: "row row-wrap next-actions" }, ...list.map((a) => {
    const t = actionTarget(a, jobId);
    const label = actionLabel(a);
    if (t.kind === "nav") return el("a", { class: "btn btn-sm btn-ghost next-action", href: t.href, text: label, title: a.description || "" });
    return el("button", {
      class: "btn btn-sm btn-ghost next-action", text: label, title: a.description || "",
      onClick: async (ev) => {
        ev.preventDefault();
        if (t.confirm && !window.confirm(t.confirm)) return;
        try { await api.req(t.endpoint, { method: t.method }); toast(`${label}: done`, "ok"); onDone?.(); }
        catch (e) { toast(String(e?.message || e), "err"); }
      },
    });
  }));
}

/** Extract an assist session id from a job's next_actions (endpoint /assist/<id>/...). */
export function assistSessionFromActions(actions) {
  for (const a of actions || []) {
    const m = /\/assist\/([0-9a-f-]{36})/i.exec(a.endpoint || a.command || "");
    if (m) return m[1];
  }
  return null;
}

/** A small link-styled button for row/card actions. */
export function actionLink(label, href, opts = {}) {
  return el("a", {
    class: "btn btn-sm" + (opts.primary ? " btn-primary" : " btn-ghost"),
    href,
    text: label,
    ...(opts.title ? { title: opts.title } : {}),
  });
}

// §17.1118 (Phase 1 ledger U-11) — focus management for anything that pops
// over the page. The replan modal and the plan drawer had no dialog role, no
// focus move, no Tab trap, no Escape, and no return-focus. This is the one
// helper both use: it marks the element as a dialog, moves focus to its first
// focusable, keeps Tab inside (modal) or just handles Escape (non-modal), and
// hands focus back to wherever it came from on close.
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function focusables(root) {
  return root ? Array.from(root.querySelectorAll(FOCUSABLE)).filter((n) => !n.hidden && n.offsetParent !== null || n === document.activeElement) : [];
}

/** Pure: the index Tab (or Shift+Tab) lands on inside a trap of `count` items. */
export function nextFocusIndex(current, count, backwards) {
  if (count <= 0) return -1;
  if (current < 0) return backwards ? count - 1 : 0;
  return backwards ? (current - 1 + count) % count : (current + 1) % count;
}

export function openDialog(node, { label, onClose, trap = true, initialFocus } = {}) {
  const opener = typeof document !== "undefined" ? document.activeElement : null;
  node.setAttribute("role", "dialog");
  if (trap) node.setAttribute("aria-modal", "true");
  if (label) node.setAttribute("aria-label", label);
  if (!node.hasAttribute("tabindex")) node.setAttribute("tabindex", "-1");
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); if (onClose) onClose(); return; }
    if (trap && e.key === "Tab") {
      const items = focusables(node);
      if (!items.length) { e.preventDefault(); node.focus(); return; }
      const i = items.indexOf(document.activeElement);
      const next = items[nextFocusIndex(i, items.length, e.shiftKey)];
      if (i === -1 || (e.shiftKey && i === 0) || (!e.shiftKey && i === items.length - 1)) { e.preventDefault(); next.focus(); }
    }
  };
  node.addEventListener("keydown", onKey);
  const first = initialFocus || focusables(node)[0] || node;
  try { first.focus(); } catch (e) { console.debug("openDialog: initial focus failed", e); }
  let closed = false;
  return {
    close() {
      if (closed) return;
      closed = true;
      node.removeEventListener("keydown", onKey);
      if (opener && typeof opener.focus === "function" && document.contains(opener)) {
        try { opener.focus(); } catch (e) { console.debug("openDialog: focus restore failed", e); }
      }
    },
  };
}

