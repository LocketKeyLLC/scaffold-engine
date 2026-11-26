// §17.2031 — a step that failed its retries holds the job; the graph says why ON the node, with the verbs that move it.
//
// §17.1572 parked it (2026-10-15): a job that went `blocked` on a step's third failed retry showed the status on the
// DAG -- where an auto run is watched (§17.1513) -- and nothing else. The node's `last_verification_reason` held the why;
// `/nodes` never carried it and the drawer had a panel only for a step the engine is parked on (awaiting_decision).
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { _m: new Map(), getItem(k) { return this._m.get(k) ?? null; }, setItem(k, v) { this._m.set(k, v); }, removeItem(k) { this._m.delete(k); } };
const noopEl = () => ({ appendChild() {}, append() {}, remove() {}, setAttribute() {}, addEventListener() {}, removeEventListener() {},
  querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {} }, style: {}, dataset: {}, children: [], insertBefore() {} });
globalThis.document = { createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, body: noopEl(), documentElement: noopEl() };
globalThis.window = { location: { hash: "" }, addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { failedStepModel, stoppedOnKey } = await import("../../app/ui/static/views/plan.js");

// the live shape: a chain where T5 failed three times and T6-T8 wait on it; T2 failed too but nothing waits on it
const NODES = [
  { node_key: "T1", status: "done", execution_order: 1, depends_on: [] },
  { node_key: "T2", status: "failed", execution_order: 2, depends_on: ["T1"], failure_reason: "" },
  { node_key: "T5", status: "failed", execution_order: 5, depends_on: ["T1"],
    failure_reason: "verifier: 0.31 -- the sandbox ran 18 tests, 4 failed (MagicMock awaited)" },
  { node_key: "T6", status: "pending", execution_order: 6, depends_on: ["T5"] },
  { node_key: "T7", status: "pending", execution_order: 7, depends_on: ["T6"] },
  { node_key: "T8", status: "pending", execution_order: 8, depends_on: ["T5", "T1"] },
];

test("a failed step reads its reason and the three verbs", () => {
  const m = failedStepModel(NODES[2], "blocked", NODES);
  assert.equal(m.reason, NODES[2].failure_reason);
  assert.deepEqual(m.verbs.map((v) => v.key), ["retry", "edit", "skip"]);
  assert.deepEqual(m.verbs[0].body, { node_key: "T5" });
  assert.equal(m.verbs[0].endpoint, "/exec/retry");
  assert.equal(m.verbs[2].endpoint, "/skip");
  assert.deepEqual(m.holds, ["T6", "T7", "T8"], "everything downstream of it, transitively");
  assert.match(m.headline, /holds 3 steps/);
});

test("a failure with no recorded reason says so, and one that holds nothing says that", () => {
  const m = failedStepModel(NODES[1], "blocked", NODES);
  assert.match(m.reason, /No reason was recorded/);
  assert.deepEqual(m.holds, []);
  assert.doesNotMatch(m.headline, /holds/);
});

test("no panel for a step that did not fail", () => {
  for (const n of NODES.filter((x) => x.status !== "failed")) assert.equal(failedStepModel(n, "blocked", NODES), null);
  assert.equal(failedStepModel(null, "blocked", NODES), null);
});

test("the graph opens on the failure that holds the most, only when the job stopped", () => {
  assert.equal(stoppedOnKey(NODES, "blocked"), "T5", "T5 holds three steps; T2 holds none");
  assert.equal(stoppedOnKey(NODES, "failed"), "T5");
  for (const st of ["running", "executing", "awaiting_decision", "completed", "cancelled", null])
    assert.equal(stoppedOnKey(NODES, st), null, `not on ${st}`);
  assert.equal(stoppedOnKey(NODES.filter((n) => n.status !== "failed"), "blocked"), null);
});
