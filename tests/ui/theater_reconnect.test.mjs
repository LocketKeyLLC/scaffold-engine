// §17.1113 (Phase 1 ledger U-3) — a dropped run stream reconnects to the
// detached run instead of pretending the run finished.
import { test } from "node:test";
import assert from "node:assert/strict";

// theater.js pulls notify.js, which touches `document` at import — stub just
// enough for a module load (same shape as job_hub_tabs.test.mjs); the
// functions under test are pure.
globalThis.localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.get(k) ?? null; },
  setItem(k, v) { this._m.set(k, v); },
  removeItem(k) { this._m.delete(k); },
};
const noopEl = () => ({
  appendChild() {}, append() {}, remove() {}, setAttribute() {},
  addEventListener() {}, removeEventListener() {}, querySelector: () => null,
  querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {} },
  style: {}, dataset: {}, children: [], insertBefore() {},
});
globalThis.document = {
  createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl,
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
  body: noopEl(), documentElement: noopEl(),
};
globalThis.window = { location: { hash: "" }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { streamDropDecision, framesAfter, RECONNECT_DELAYS_MS } =
  await import("../../app/ui/static/views/theater.js");

test("while the run is live the tab re-attaches; when it is not, the tab finishes", () => {
  assert.equal(streamDropDecision({ detached_running: true, job_status: "running" }, 1, 5), "reattach");
  assert.equal(streamDropDecision({ detached_running: false, job_status: "completed" }, 1, 5), "finished");
  assert.equal(streamDropDecision({ detached_running: false, job_status: "running" }, 3, 5), "finished",
    "a row that says running with no live task is the restart case — not something to attach to");
});

test("unreachable engine: retry until the last attempt, then give up", () => {
  assert.equal(streamDropDecision(null, 1, 5), "retry");
  assert.equal(streamDropDecision(null, 4, 5), "retry");
  assert.equal(streamDropDecision(null, 5, 5), "give_up");
  assert.equal(streamDropDecision(undefined, 5, 5), "give_up");
});

test("backoff grows and stays bounded", () => {
  assert.ok(RECONNECT_DELAYS_MS.length >= 3);
  for (let i = 1; i < RECONNECT_DELAYS_MS.length; i++) assert.ok(RECONNECT_DELAYS_MS[i] > RECONNECT_DELAYS_MS[i - 1]);
  assert.ok(RECONNECT_DELAYS_MS.reduce((a, b) => a + b, 0) <= 60_000, "a full reconnect cycle fits in a minute");
});

test("a replayed backlog skips the frames this tab already rendered", () => {
  const frames = ["a", "b", "c", "d"];
  assert.deepEqual(framesAfter(frames, 0), frames);
  assert.deepEqual(framesAfter(frames, 2), ["c", "d"]);
  assert.deepEqual(framesAfter(frames, 9), []);
  assert.deepEqual(framesAfter(frames, -1), frames);
  assert.deepEqual(framesAfter(null, 2), []);
});
