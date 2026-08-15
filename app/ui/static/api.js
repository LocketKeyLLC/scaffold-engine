// Orchestrator API client. Same-origin fetch; X-API-Key from localStorage.
//
// SSE note: every streaming endpoint (/execute/all, /research, /assist/*/stream)
// is a POST requiring X-API-Key. Native EventSource can't POST or set headers,
// so we consume text/event-stream via fetch + a ReadableStream reader (see
// `stream()` below). This is the canonical way to drive the live views.

const KEY_STORAGE = "scaffold_api_key";

export function getKey() {
  return localStorage.getItem(KEY_STORAGE) || "";
}
export function setKey(k) {
  if (k) localStorage.setItem(KEY_STORAGE, k);
  else localStorage.removeItem(KEY_STORAGE);
}
export function hasKey() {
  return !!getKey();
}

function authHeaders(extra = {}) {
  const h = { ...extra };
  const k = getKey();
  if (k) h["X-API-Key"] = k;
  return h;
}

export class ApiError extends Error {
  constructor(status, detail, body) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

async function parseError(resp) {
  let body = null;
  let detail = resp.statusText;
  try {
    body = await resp.json();
    detail = body?.detail || body?.error || JSON.stringify(body);
  } catch {
    try {
      detail = (await resp.text()) || resp.statusText;
    } catch {
      /* ignore */
    }
  }
  return new ApiError(resp.status, detail, body);
}

/** JSON request. Returns parsed body (or null for 204). Throws ApiError. */
export async function req(path, { method = "GET", body, signal, query } = {}) {
  let url = path;
  if (query) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v != null && v !== "") q.set(k, v);
    }
    const s = q.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }
  const headers = authHeaders(
    body != null ? { "Content-Type": "application/json" } : {}
  );
  const resp = await fetch(url, {
    method,
    headers,
    body: body != null ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  if (resp.status === 204) return null;
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp.text();
}

export const get = (p, opts) => req(p, { ...opts, method: "GET" });
export const post = (p, body, opts) => req(p, { ...opts, method: "POST", body });
export const patch = (p, body, opts) => req(p, { ...opts, method: "PATCH", body });
export const del = (p, opts) => req(p, { ...opts, method: "DELETE" });

/**
 * Consume an SSE endpoint as an async iterator of {event, data} objects.
 * `data` is JSON-parsed when possible, else the raw string. Keepalive comment
 * frames (": ...") are skipped. Honors an AbortSignal for cancellation.
 *
 * Usage:
 *   for await (const {event, data} of stream("/execute/all", {body:{job_id}})) { ... }
 */
export async function* stream(path, { method = "POST", body, signal } = {}) {
  const resp = await fetch(path, {
    method,
    headers: authHeaders({
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    }),
    body: body != null ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!resp.ok) throw await parseError(resp);
  if (!resp.body) throw new ApiError(0, "No response body for stream");

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      // SSE frames are separated by a blank line. Handle \n\n and \r\n\r\n.
      while ((sep = nextFrameBreak(buf)) !== -1) {
        const [end, skip] = sep;
        const raw = buf.slice(0, end);
        buf = buf.slice(end + skip);
        const evt = parseFrame(raw);
        if (evt) yield evt;
      }
    }
    const tail = parseFrame(buf);
    if (tail) yield tail;
  } finally {
    try {
      await reader.cancel();
    } catch {
      /* already closed */
    }
  }
}

function nextFrameBreak(buf) {
  const a = buf.indexOf("\n\n");
  const b = buf.indexOf("\r\n\r\n");
  if (a === -1 && b === -1) return -1;
  if (b !== -1 && (a === -1 || b < a)) return [b, 4];
  return [a, 2];
}

function parseFrame(raw) {
  if (!raw || !raw.trim()) return null;
  let event = "message";
  const dataLines = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue; // comment / keepalive
    const idx = line.indexOf(":");
    const field = idx === -1 ? line : line.slice(0, idx);
    let val = idx === -1 ? "" : line.slice(idx + 1);
    if (val.startsWith(" ")) val = val.slice(1);
    if (field === "event") event = val;
    else if (field === "data") dataLines.push(val);
  }
  if (dataLines.length === 0) return null;
  const dataStr = dataLines.join("\n");
  let data = dataStr;
  try {
    data = JSON.parse(dataStr);
  } catch {
    /* keep raw string */
  }
  return { event, data };
}

// ── Health (unauthenticated) ──────────────────────────────────────────
export async function health() {
  const resp = await fetch("/health");
  return resp.json();
}

/** Probe the key by hitting a cheap authenticated endpoint. Returns true on 200. */
export async function validateKey() {
  try {
    await get("/jobs", { query: { limit: 1 } });
    return true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return false;
    throw e; // network / other — surface separately
  }
}
