// §17.815 — api.js identity helpers (whoami cache, 404 fallback, validateKey,
// 401 signal). fetch + localStorage are stubbed; no browser needed.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.get(k) ?? null; },
  setItem(k, v) { this._m.set(k, v); },
  removeItem(k) { this._m.delete(k); },
};

const api = await import("../../app/ui/static/api.js");

function jsonResp(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    headers: { get: () => "application/json" },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

beforeEach(() => { globalThis.fetch = undefined; });

test("whoami caches the principal", async () => {
  globalThis.fetch = async () =>
    jsonResp(200, { identity: "alice", role: "user", is_admin: false, key_id: 7, multi_user: true });
  const p = await api.whoami();
  assert.equal(p.identity, "alice");
  assert.equal(api.principal().is_admin, false);
});

test("whoami falls back to admin default on a pre-§17.815 server (404)", async () => {
  globalThis.fetch = async () => jsonResp(404, { detail: "Not Found" });
  const p = await api.whoami();
  assert.equal(p.identity, "admin");
  assert.equal(p.is_admin, true);
});

test("validateKey true on success, false on 401", async () => {
  globalThis.fetch = async () =>
    jsonResp(200, { identity: "a", role: "admin", is_admin: true });
  assert.equal(await api.validateKey(), true);
  globalThis.fetch = async () => jsonResp(401, { detail: "bad key" });
  assert.equal(await api.validateKey(), false);
});

test("validateKey rethrows network errors", async () => {
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  await assert.rejects(() => api.validateKey(), TypeError);
});

test("a 401 outside the browser does not crash (window guard)", async () => {
  globalThis.fetch = async () => jsonResp(401, { detail: "nope" });
  await assert.rejects(() => api.get("/jobs"), (e) => e.status === 401);
});
