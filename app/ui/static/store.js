// §17.1115 (Phase 1 ledger U-4) — the job store.
//
// The SPA had no client-side store: the plan tab's first paint issued FOUR
// `GET /jobs/{id}` (hub, plan ×2, brief panel), the overview two, and every
// view kept its own copy of the job. This is the smallest store that fixes
// that without restructuring the views: one `get(jobId)` that COALESCES
// concurrent requests for the same job into one fetch, caches the answer for
// a few seconds, and forgets it the moment anything could have changed it —
// any mutating API call (api.js dispatches `scaffold:mutated` after every
// non-GET) or a `scaffold:job-status` event. Views that must see the row as
// it is right now (a poll waiting for a status change) pass `fresh: true`.
//
// `createStore` is exported so the node tests can inject a fetcher and a
// clock; `jobStore` is the app's instance.
import * as api from "./api.js";

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

export function idsIn(path) {
  return String(path || "").match(UUID_RE) || [];
}

export function createStore({ fetcher, ttlMs = 5000, now = () => Date.now(), target } = {}) {
  if (typeof fetcher !== "function") throw new TypeError("createStore: fetcher is required");
  const cache = new Map();     // id → { value, at }
  const inflight = new Map();  // id → Promise
  const tgt = target !== undefined ? target : (typeof window !== "undefined" ? window : null);

  function invalidate(id) {
    if (id == null) cache.clear();
    else cache.delete(String(id));
  }

  function get(id, { fresh = false } = {}) {
    const k = String(id);
    if (!fresh) {
      const c = cache.get(k);
      if (c && now() - c.at < ttlMs) return Promise.resolve(c.value);
    }
    // An in-flight request is as fresh as a new one would be — join it.
    if (inflight.has(k)) return inflight.get(k);
    // The fetcher is invoked SYNCHRONOUSLY so a second get() in the same tick
    // finds the in-flight entry — that is the whole coalescing guarantee.
    let started;
    try { started = Promise.resolve(fetcher(k)); } catch (err) { started = Promise.reject(err); }
    const p = started
      .then((value) => {
        cache.set(k, { value, at: now() });
        inflight.delete(k);
        return value;
      }, (err) => {
        inflight.delete(k);
        throw err;                                  // errors are never cached
      });
    inflight.set(k, p);
    return p;
  }

  function peek(id) {
    const c = cache.get(String(id));
    return c ? c.value : null;
  }

  if (tgt && typeof tgt.addEventListener === "function") {
    // A status change means the row changed in ways the event does not carry
    // (completed_at, compiled_output, …): forget it, the next get refetches.
    tgt.addEventListener("scaffold:job-status", (ev) => {
      const d = (ev && ev.detail) || {};
      invalidate(d.jobId != null ? d.jobId : undefined);
    });
    // Any mutation may touch any job (a session id in the path is not a job
    // id): forget everything. The cache is a handful of rows; correctness wins.
    tgt.addEventListener("scaffold:mutated", () => invalidate());
  }

  return { get, peek, invalidate, size: () => cache.size };
}

export const jobStore = createStore({ fetcher: (id) => api.get(`/jobs/${id}`) });
