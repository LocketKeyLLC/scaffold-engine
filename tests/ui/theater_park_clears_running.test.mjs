// §17.1337 — when the run parks or ends, nothing is running.
//
// Live, 2026-10-04: the operator read "ADD122 is running" on the job page while
// the engine's own /exec/status said that step was pending, no node was in the
// running state, and the parked step was a different one. `node_start` marks a
// node running and only `node_done` / `node_failed` clear it, so a step the
// engine previewed and then parked instead of claiming stayed on screen as
// running for good.
import { test } from "node:test";
import assert from "node:assert/strict";

// theater.js pulls notify.js, which touches `document` at import — stub just
// enough for a module load (same shape as theater_reconnect.test.mjs); the
// function under test is pure.
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
  body: noopEl(), documentElement: noopEl(), hidden: false, visibilityState: "visible",
};
globalThis.window = { addEventListener() {}, removeEventListener() {}, location: { hash: "" },
                      matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) };
globalThis.Notification = undefined;

const { clearsRunning, CLEARS_RUNNING } = await import("../../app/ui/static/views/theater.js");

test("a park clears what the page shows as running", () => {
  assert.equal(clearsRunning("awaiting_decision"), true);
  assert.equal(clearsRunning("awaiting_assist"), true);
});

test("so does every way a run ends", () => {
  for (const e of ["pipeline_complete", "execution_failed", "error", "budget_exhausted"]) {
    assert.equal(clearsRunning(e), true, e);
  }
});

test("the events of a run in progress clear nothing", () => {
  for (const e of ["node_start", "node_token", "node_done", "node_failed", "node_retry",
                   "progress", "queued", "dag_generated"]) {
    assert.equal(clearsRunning(e), false, e);
  }
});

test("junk is not a park", () => {
  assert.equal(clearsRunning(""), false);
  assert.equal(clearsRunning(undefined), false);
  assert.equal(clearsRunning(null), false);
  assert.equal(clearsRunning("AWAITING_DECISION"), false, "the stream's own spelling only");
});

test("every clearing event is one the stream actually sends", () => {
  // the set is the stream's vocabulary, not a wish list
  const sent = new Set(["awaiting_decision", "awaiting_assist", "pipeline_complete",
                         "execution_failed", "error", "budget_exhausted"]);
  for (const e of CLEARS_RUNNING) assert.equal(sent.has(e), true, e);
});
