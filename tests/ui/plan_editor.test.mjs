// UX overhaul phase 5 — the step editor's pure parts: generated keys, and a
// plain label for every tool the engine accepts (app/config.py VALID_TOOLS).
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { _m: new Map(), getItem(k) { return this._m.get(k) ?? null; }, setItem(k, v) { this._m.set(k, v); }, removeItem(k) { this._m.delete(k); } };
const noopEl = () => ({ appendChild() {}, append() {}, remove() {}, setAttribute() {}, addEventListener() {}, removeEventListener() {},
  querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {} }, style: {}, dataset: {}, children: [], insertBefore() {} });
globalThis.document = { createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, body: noopEl(), documentElement: noopEl() };
globalThis.window = { location: { hash: "" }, addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { TOOL_LABELS, nextNodeKey } = await import("../../app/ui/static/views/plan.js");
const { AFTER_APPROVE, approveBodyFor, approveTailFor } = await import("../../app/ui/static/views/approvals.js");

// Mirror of app/config.py VALID_TOOLS.
const VALID_TOOLS = ["LLM", "CodeGen", "SearXNG", "Milvus", "Shell", "MCP"];

test("every tool the engine accepts has a plain label, and nothing else does", () => {
  assert.deepEqual(Object.keys(TOOL_LABELS).sort(), [...VALID_TOOLS].sort());
  for (const v of Object.values(TOOL_LABELS)) assert.ok(!/^[A-Z][A-Za-z]+$/.test(v), `${v} is still just the enum`);
});

test("the next key is one above the highest T-key, skipping anything taken", () => {
  assert.equal(nextNodeKey([]), "T1");
  assert.equal(nextNodeKey(["T1", "T2", "T3"]), "T4");
  assert.equal(nextNodeKey(["T1", "ADD65", "ADD93", "T37"]), "T38");
  assert.equal(nextNodeKey(["T1", "T3"]), "T4", "gaps are not reused — order is the plan's memory");
  assert.equal(nextNodeKey(["X1", "ADD2"]), "T1");
});

test("the gate's three outcomes map to the approve body, review-first stops at the plan", () => {
  assert.deepEqual(AFTER_APPROVE.map(([k]) => k), ["review", "walk", "run"]);
  assert.deepEqual(approveBodyFor("review"), { assist: false, execute: false });
  assert.deepEqual(approveBodyFor("walk"), { assist: true, execute: false });
  assert.deepEqual(approveBodyFor("run"), { assist: false, execute: true });
  assert.deepEqual(approveBodyFor("garbage"), { assist: false, execute: false }, "an unknown choice never starts anything");
  assert.match(approveTailFor("review"), /research & plan$/);
  assert.match(approveTailFor("walk"), /walkthrough/);
  assert.match(approveTailFor("run"), /run it/);
});
