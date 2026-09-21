// §17.1160 — the Follow rail model: one ordered list, folded around the current step.
import { test } from "node:test";
import assert from "node:assert/strict";

// job_hub.js pulls the whole view tree, which touches `document` at import — the same
// stub tests/ui/job_hub_tabs.test.mjs uses; the model under test is pure.
globalThis.localStorage = { _m: new Map(), getItem(k) { return this._m.get(k) ?? null; }, setItem(k, v) { this._m.set(k, v); }, removeItem(k) { this._m.delete(k); } };
const noopEl = () => ({ appendChild() {}, append() {}, remove() {}, setAttribute() {}, addEventListener() {}, removeEventListener() {},
  querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {} }, style: {}, dataset: {}, children: [], insertBefore() {} });
globalThis.document = { createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, body: noopEl(), documentElement: noopEl() };
globalThis.window = { location: { hash: "" }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { railModel } = await import("../../app/ui/static/views/assist.js");
const { KNOWN_TABS, resolveTab, TAB_ALIASES } = await import("../../app/ui/static/views/job_hub.js");

const steps = [
  { node_key: "T1", title: "Confirm hardware", step_status: "committed" },
  { node_key: "T2", title: "Install Proxmox", step_status: "committed" },
  { node_key: "T3", title: "Create the bridge", step_status: "skipped" },
  { node_key: "ADD76", title: "Install the local runner helper", step_status: "committed" },
  { node_key: "ADD80", title: "Refresh the local runner helper (one paste)", step_status: "presented" },
  { node_key: "ADD49", title: "Make VM 110 reachable on SSH", step_status: "pending" },
  { node_key: "T5", title: "An earlier-ordered step still open", step_status: "pending" },
  { node_key: "T6", title: "Configure Proxmox firewall rules", step_status: "pending" },
  { node_key: "T7", title: "Install Jellyfin", step_status: "pending" },
  { node_key: "T8", title: "Install Radarr", step_status: "pending" },
  { node_key: "T9", title: "Install Sonarr", step_status: "pending" },
  { node_key: "T10", title: "Document", step_status: "pending" },
];

test("folds finished and far-ahead steps around the current one, keeps counts honest", () => {
  const m = railModel(steps, "ADD80");
  assert.equal(m.current.node_key, "ADD80");
  assert.deepEqual(m.done.map((r) => r.node_key), ["T3", "ADD76"]);       // the last two finished stay visible
  assert.equal(m.doneHidden, 2);                                          // T1, T2 fold to a count
  assert.deepEqual(m.ahead.map((r) => r.node_key), ["ADD49", "T5", "T6", "T7"]);
  assert.equal(m.aheadHidden, 3);                                         // T8, T9, T10
  assert.equal(m.total, 12);
  assert.equal(m.doneCount, 4);                                           // committed + skipped are terminal
  assert.ok(m.current.current && !m.current.terminal);
  assert.ok(m.done.find((r) => r.node_key === "ADD76").inserted);         // engine-inserted steps are marked
  assert.ok(!m.done.find((r) => r.node_key === "T3").inserted);
});

test("a pending step earlier in the plan's order is still AHEAD, never counted done", () => {
  const list = [
    { node_key: "T1", title: "a", step_status: "pending" },       // earlier in order, still open
    { node_key: "T2", title: "b", step_status: "committed" },
    { node_key: "T3", title: "c", step_status: "presented" },     // current
    { node_key: "T4", title: "d", step_status: "pending" },
  ];
  const m = railModel(list, "T3");
  assert.deepEqual(m.done.map((r) => r.node_key), ["T2"]);
  assert.deepEqual(m.ahead.map((r) => r.node_key), ["T1", "T4"]);
  assert.equal(m.doneCount, 1);
  assert.equal(m.doneHidden + m.done.length, m.doneCount);                // the fold count agrees with the header
});

test("expanding shows everything; a focused folded step is surfaced", () => {
  const m = railModel(steps, "ADD80", { expandDone: true, expandAhead: true });
  assert.equal(m.doneHidden, 0);
  assert.equal(m.done.length, 4);
  assert.equal(m.aheadHidden, 0);
  const f = railModel(steps, "ADD80", { focus: "T1" });
  assert.equal(f.done[0].node_key, "T1");                                  // pulled out of the fold
  assert.ok(f.done[0].focused);
  assert.equal(f.doneHidden, 1);                                           // only T2 stays hidden
});

test("no current step: finished steps before, pending after; empty input is safe", () => {
  const m = railModel(steps, null);
  assert.equal(m.current, null);
  assert.equal(m.doneCount, 4);
  assert.equal(m.ahead.length, 4);
  const e = railModel([], "X");
  assert.deepEqual([e.done, e.ahead, e.current, e.total], [[], [], null, 0]);
});

test("the hub knows the follow tab", () => {
  assert.ok(KNOWN_TABS.includes("follow") && KNOWN_TABS.includes("full"));   // §17.1161 — run IS follow; full is the classic page
  assert.equal(resolveTab("follow"), "follow");
  assert.equal(resolveTab("run"), "run");
  assert.equal(TAB_ALIASES.assist, "run");                                 // the Run aliases are untouched
});
