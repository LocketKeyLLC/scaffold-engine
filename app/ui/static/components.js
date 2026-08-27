// Reusable UI components shared across views.
import { el } from "./util.js";

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
  if (opts.onClick) tile.addEventListener("click", opts.onClick);
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
