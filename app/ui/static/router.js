// Tiny hash router. Routes are "#/segment/:param" patterns.

const routes = [];
let notFound = null;
let current = null;

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
    const path = currentPath();
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
