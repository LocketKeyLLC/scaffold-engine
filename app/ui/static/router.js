// Tiny hash router. Routes are "#/segment/:param" patterns.

const routes = [];
let notFound = null;
let current = null;
// §17.1116 (ledger U-6) — a view whose in-flight work dies with it (the
// research stream cancels the session on disconnect) registers a guard:
// a function returning a message while leaving would lose something, or
// null. Navigating away then asks first; a refusal restores the hash.
let navGuard = null;
let suppressNext = false;

export function setNavGuard(fn) {
  navGuard = typeof fn === "function" ? fn : null;
}

/** Pure: may the navigation from `fromPath` to `toPath` proceed? `confirmFn`
 *  is asked only when the guard has something to lose. */
export function navAllowed(guard, fromPath, toPath, confirmFn) {
  if (!guard || fromPath === toPath) return true;
  const msg = guard(toPath);
  if (!msg) return true;
  return !!confirmFn(msg);
}

export function route(pattern, handler) {
  const parts = pattern.split("/").filter(Boolean);
  routes.push({ pattern, parts, handler });
}

export function setNotFound(handler) {
  notFound = handler;
}

function match(path) {
  const segs = path.split("/").filter(Boolean);
  for (const r of routes) {
    if (r.parts.length !== segs.length && !r.parts.some((p) => p.endsWith("?")))
      continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < r.parts.length; i++) {
      const p = r.parts[i];
      const optional = p.endsWith("?");
      const name = optional ? p.slice(1, -1) : p.startsWith(":") ? p.slice(1) : null;
      const seg = segs[i];
      if (name) {
        if (seg == null && !optional) {
          ok = false;
          break;
        }
        if (seg != null) params[name] = decodeURIComponent(seg);
      } else if (p !== seg) {
        ok = false;
        break;
      }
    }
    if (ok && (segs.length === r.parts.length || r.parts.some((p) => p.endsWith("?"))))
      return { route: r, params };
  }
  return null;
}

export function currentPath() {
  const h = location.hash.replace(/^#/, "");
  return h || "/";
}

export function navigate(path) {
  location.hash = path.startsWith("#") ? path : "#" + path;
}

/** Read the current route params (for the active view). */
export function getCurrent() {
  return current;
}

export function start() {
  const dispatch = () => {
    if (suppressNext) { suppressNext = false; return; }   // the hash restore below
    const path = currentPath();
    if (current && !navAllowed(navGuard, current.path, path, (msg) => window.confirm(msg))) {
      suppressNext = true;
      location.hash = "#" + current.path;
      return;
    }
    const m = match(path);
    if (m) {
      current = { path, params: m.params };
      m.route.handler(m.params, path);
    } else if (notFound) {
      current = { path, params: {} };
      notFound(path);
    }
  };
  window.addEventListener("hashchange", dispatch);
  dispatch();
}
