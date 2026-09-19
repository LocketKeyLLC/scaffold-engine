// §17.1115 (Phase 1 ledger U-4) — the job store coalesces, caches briefly, and
// forgets on any mutation or status change.
import { test } from "node:test";
import assert from "node:assert/strict";

// store.js imports api.js, which reads localStorage at call time only; a
// minimal window is enough for the module load + the event wiring.
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.window = new EventTarget();
globalThis.window.location = { hash: "" };

const { createStore, idsIn } = await import("../../app/ui/static/store.js");

function harness({ ttlMs = 5000 } = {}) {
  let t = 1000;
  const calls = [];
  const pending = new Map();
  const fetcher = (id) => new Promise((resolve, reject) => {
    calls.push(id);
    pending.set(id, { resolve, reject });
  });
  const target = new EventTarget();
  const store = createStore({ fetcher, ttlMs, now: () => t, target });
  return {
    store, calls, target,
    settle(id, value) { pending.get(id).resolve(value); pending.delete(id); },
    fail(id, err) { pending.get(id).reject(err); pending.delete(id); },
    tick(ms) { t += ms; },
  };
}

test("concurrent gets for one job share a single fetch", async () => {
  const h = harness();
  const a = h.store.get("j1"), b = h.store.get("j1"), c = h.store.get("j1");
  assert.deepEqual(h.calls, ["j1"], "one request in flight, not three");
  h.settle("j1", { id: "j1", status: "planning" });
  const [ra, rb, rc] = await Promise.all([a, b, c]);
  assert.equal(ra, rb); assert.equal(rb, rc);
  assert.equal(ra.status, "planning");
});

test("a cached job is served without a fetch inside the TTL, and refetched after it", async () => {
  const h = harness({ ttlMs: 5000 });
  const p = h.store.get("j1"); h.settle("j1", { id: "j1", status: "planning" }); await p;
  assert.deepEqual(await h.store.get("j1"), { id: "j1", status: "planning" });
  assert.deepEqual(h.calls, ["j1"], "served from cache");
  h.tick(6000);
  const p2 = h.store.get("j1");
  assert.deepEqual(h.calls, ["j1", "j1"], "TTL expired → refetch");
  h.settle("j1", { id: "j1", status: "executing" });
  assert.equal((await p2).status, "executing");
});

test("fresh: true bypasses the cache but still joins an in-flight request", async () => {
  const h = harness();
  const p = h.store.get("j1"); h.settle("j1", { id: "j1", status: "a" }); await p;
  const f = h.store.get("j1", { fresh: true });
  assert.deepEqual(h.calls, ["j1", "j1"]);
  const g = h.store.get("j1", { fresh: true });
  assert.deepEqual(h.calls, ["j1", "j1"], "second fresh joins the in-flight fetch");
  h.settle("j1", { id: "j1", status: "b" });
  assert.equal((await f).status, "b"); assert.equal((await g).status, "b");
});

test("a mutation forgets every cached job; a job-status event forgets that job", async () => {
  const h = harness();
  for (const id of ["j1", "j2"]) { const p = h.store.get(id); h.settle(id, { id }); await p; }
  assert.equal(h.store.size(), 2);
  h.target.dispatchEvent(new CustomEvent("scaffold:job-status", { detail: { jobId: "j1", status: "completed" } }));
  assert.equal(h.store.peek("j1"), null, "j1 forgotten");
  assert.ok(h.store.peek("j2"), "j2 kept");
  h.target.dispatchEvent(new CustomEvent("scaffold:mutated", { detail: { method: "POST", path: "/assist/s1/submit" } }));
  assert.equal(h.store.size(), 0, "any mutation clears everything");
});

test("errors are not cached and do not poison later gets", async () => {
  const h = harness();
  const p = h.store.get("j1");
  h.fail("j1", new Error("500"));
  await assert.rejects(p, /500/);
  const p2 = h.store.get("j1");
  assert.deepEqual(h.calls, ["j1", "j1"], "a failed fetch is retried on the next get");
  h.settle("j1", { id: "j1" });
  assert.deepEqual(await p2, { id: "j1" });
});

test("idsIn finds job ids in a path", () => {
  assert.deepEqual(idsIn("/jobs/702bc079-3ea1-4fc7-80b0-127cfdc3b243/cancel"), ["702bc079-3ea1-4fc7-80b0-127cfdc3b243"]);
  assert.deepEqual(idsIn("/execute/all"), []);
});
