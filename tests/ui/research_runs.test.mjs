// §17.1120 — detached research runs: the pure decisions the view makes.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const noopEl = () => ({ appendChild() {}, append() {}, remove() {}, setAttribute() {}, addEventListener() {}, removeEventListener() {},
  querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {}, replace() {} }, style: {}, dataset: {}, children: [], insertBefore() {} });
globalThis.document = { createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, body: noopEl(), documentElement: noopEl() };
globalThis.window = { location: { hash: "" }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { RUN_KEY, runStreamPath, shouldReattach, runDropDecision, RECONNECT_DELAYS_MS } =
  await import("../../app/ui/static/views/research.js");

test("the stream path and the storage key are stable", () => {
  assert.equal(runStreamPath("abc"), "/research/runs/abc/stream");
  assert.equal(RUN_KEY, "scaffold_research_run");
});

test("after a reload, re-attach only to a run the engine says is running", () => {
  assert.equal(shouldReattach({ running: true }), true);
  assert.equal(shouldReattach({ running: false }), false);
  assert.equal(shouldReattach(null), false);
  assert.equal(shouldReattach("running"), false);
});

test("stream-drop decisions match the theater's table", () => {
  assert.equal(runDropDecision({ running: true }, 1, 5), "reattach");
  assert.equal(runDropDecision({ running: false }, 1, 5), "finished");
  assert.equal(runDropDecision(null, 2, 5), "retry");
  assert.equal(runDropDecision(null, 5, 5), "give_up");
  assert.ok(RECONNECT_DELAYS_MS.reduce((a, b) => a + b, 0) <= 60000);
});
