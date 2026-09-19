// §17.1122 — the shell reads (health/status/account/first-run) are memoized:
// one in-flight request per key, a short TTL, cleared by any mutation.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const api = await import("../../app/ui/static/api.js");

function harness() {
  let t = 1000; const calls = []; const pending = [];
  const fn = () => new Promise((resolve, reject) => { calls.push(1); pending.push({ resolve, reject }); });
  return { fn, calls, pending, now: () => t, tick: (ms) => { t += ms; } };
}

test("concurrent callers share one in-flight request", async () => {
  api.clearMemo();
  const h = harness();
  const a = api.memoized("k", 5000, h.fn, h.now), b = api.memoized("k", 5000, h.fn, h.now);
  assert.equal(h.calls.length, 1);
  h.pending[0].resolve({ ok: 1 });
  assert.deepEqual(await a, { ok: 1 }); assert.deepEqual(await b, { ok: 1 });
});

test("served from memo inside the TTL, refetched after it", async () => {
  api.clearMemo();
  const h = harness();
  const p = api.memoized("k", 5000, h.fn, h.now); h.pending[0].resolve("v1"); await p;
  assert.equal(await api.memoized("k", 5000, h.fn, h.now), "v1");
  assert.equal(h.calls.length, 1);
  h.tick(6000);
  const p2 = api.memoized("k", 5000, h.fn, h.now);
  assert.equal(h.calls.length, 2);
  h.pending[1].resolve("v2");
  assert.equal(await p2, "v2");
});

test("clearMemo forgets (a mutation calls it); a failed fetch keeps the old value and is retried", async () => {
  api.clearMemo();
  const h = harness();
  const p = api.memoized("k", 5000, h.fn, h.now); h.pending[0].resolve("v1"); await p;
  api.clearMemo();
  const p2 = api.memoized("k", 5000, h.fn, h.now);
  assert.equal(h.calls.length, 2);
  h.pending[1].reject(new Error("503"));
  await assert.rejects(p2, /503/);
  const p3 = api.memoized("k", 5000, h.fn, h.now);
  assert.equal(h.calls.length, 3, "an error is not memoized");
  h.pending[2].resolve("v3");
  assert.equal(await p3, "v3");
});

test("the shell helpers exist and carry TTLs", () => {
  for (const f of ["health", "status", "firstRun", "accountStatus"]) assert.equal(typeof api[f], "function");
  assert.ok(api.MEMO_TTL_MS.health >= 3000 && api.MEMO_TTL_MS.status >= 2000 && api.MEMO_TTL_MS.account >= 30000);
});
