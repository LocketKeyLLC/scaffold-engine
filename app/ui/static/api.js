import { storage } from "./storage.js";
// Orchestrator API client. Same-origin fetch; X-API-Key from localStorage.
//
// SSE note: every streaming endpoint (/execute/all, /research, /assist/*/stream)
// is a POST requiring X-API-Key. Native EventSource can't POST or set headers,
// so we consume text/event-stream via fetch + a ReadableStream reader (see
// `stream()` below). This is the canonical way to drive the live views.

const KEY_STORAGE = "scaffold_api_key";

export function getKey() {
  return storage.get(KEY_STORAGE) || "";
}
export function setKey(k) {
  if (k) storage.set(KEY_STORAGE, k);
  else storage.remove(KEY_STORAGE);
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
  if (!resp.ok) {
    _maybeSignalUnauthorized(resp.status);
    throw await parseError(resp);
  }
  // §17.1115 — every successful mutation announces itself, so the job store
  // (store.js) can forget cached rows. This is the single funnel all API
  // calls pass through; views never have to remember to invalidate.
  if (method !== "GET") {
    clearMemo();   // §17.1122 — a write may have changed any shell read
    if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
      try { window.dispatchEvent(new CustomEvent("scaffold:mutated", { detail: { method, path } })); } catch { /* never break a call */ }
    }
  }
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
  if (!resp.ok) {
    _maybeSignalUnauthorized(resp.status);
    throw await parseError(resp);
  }
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

// ── §17.1122 — memoized shell reads ───────────────────────────────────
// Every first paint issued the same handful of requests more than once:
// /health twice (the sidebar poll's first tick + the dashboard), /status
// twice (the attention poll + the dashboard), /auth/account/status two or
// three times (boot, the first-run check, the dashboard). These are the
// app's "shell" reads: one in-flight request per key is shared by every
// caller, and the answer is reused for a short TTL. Any mutation clears
// the memo (see req()), so nothing stale survives a write.
const _memo = new Map();   // key → { value, at, inflight }

export function memoized(key, ttlMs, fn, now = () => Date.now()) {
  const m = _memo.get(key);
  if (m && m.inflight) return m.inflight;
  if (m && m.at != null && now() - m.at < ttlMs) return Promise.resolve(m.value);
  let p;
  try { p = Promise.resolve(fn()); } catch (e) { p = Promise.reject(e); }
  const entry = { value: m ? m.value : undefined, at: m ? m.at : null, inflight: null };
  entry.inflight = p.then((v) => { _memo.set(key, { value: v, at: now(), inflight: null }); return v; },
                          (e) => { _memo.set(key, { value: entry.value, at: entry.at, inflight: null }); throw e; });
  _memo.set(key, entry);
  return entry.inflight;
}

export function clearMemo(key) {
  if (key == null) _memo.clear(); else _memo.delete(key);
}

export const MEMO_TTL_MS = { health: 8000, status: 4000, account: 60000, firstRun: 60000 };

// ── Health (unauthenticated) ──────────────────────────────────────────
export function health() {
  return memoized("health", MEMO_TTL_MS.health, async () => {
    const resp = await fetch("/health");
    return resp.json();
  });
}

/** The job overview (`/status`): shared by the dashboard and the attention poll. */
export function status() {
  return memoized("status", MEMO_TTL_MS.status, () => get("/status"));
}

/** First-run state (`/meta/first-run`), read once per minute at most. */
export function firstRun() {
  return memoized("firstRun", MEMO_TTL_MS.firstRun, () => get("/meta/first-run"));
}

// ── Admin account (§17.840 — password unlocks the console) ────────────

/** Public: {claimed, display_name, login_available}. Null on any failure
 *  (pre-§17.840 server) so the gate falls back to key-paste. */
export function accountStatus() {
  return memoized("account", MEMO_TTL_MS.account, async () => {
    try {
      const resp = await fetch("/auth/account/status");
      return resp.ok ? await resp.json() : null;
    } catch {
      return null;
    }
  });
}

/** Password → console credential. Throws ApiError (401 wrong password,
 *  429 throttled). On success the key is stored like a manual paste. */
export async function login(password) {
  const resp = await fetch("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!resp.ok) throw await parseError(resp);
  const out = await resp.json();
  if (out?.api_key) setKey(out.api_key);
  clearMemo();   // §17.1122
  return out;
}

/** Create/replace the admin account (requires the current session's key). */
export const setupAccount = (display_name, password) =>
  post("/auth/account/setup", { display_name, password });

// ── Identity (§17.815 / plan 5.3) ─────────────────────────────────────

// §17.815 — a 401 mid-session (rotated/revoked key) routes the operator back
// to the connect gate instead of every view failing with opaque toasts.
// Dispatched as an event so api.js stays UI-free; app.js owns the response.
// Guarded for non-browser contexts (the node test lane).
function _maybeSignalUnauthorized(status) {
  if (status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("scaffold:unauthorized"));
  }
}

let _principal = null;

/** The cached /auth/whoami result ({identity, role, is_admin, key_id,
 *  multi_user}) or null before login. */
export function principal() {
  return _principal;
}

/** Fetch + cache the caller's identity. Falls back to the single-user admin
 *  default on a pre-§17.815 server (404) so the SPA still works there. */
export async function whoami() {
  try {
    _principal = await get("/auth/whoami");
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      _principal = {
        identity: "admin", role: "admin", is_admin: true,
        key_id: null, multi_user: false,
      };
    } else {
      throw e;
    }
  }
  return _principal;
}

/** Probe the key via /auth/whoami (also caches identity). True on success,
 *  false on 401; other failures (network) throw for separate surfacing. */
export async function validateKey() {
  try {
    await whoami();
    return true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return false;
    throw e; // network / other — surface separately
  }
}

// §17.818 (plan 5.8) — single-source domain list for pickers, cached per
// session. Falls back to the historical constant on a pre-§17.818 server.
let _domains = null;
export async function domains() {
  if (_domains) return _domains;
  try {
    const res = await get("/meta/domains");
    _domains = res.domains || [];
  } catch {
    _domains = ["prompt", "rag", "llm", "spec", "eng", "eng_design"];
  }
  return _domains;
}
