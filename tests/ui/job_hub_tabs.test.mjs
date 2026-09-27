// The job page's panes (UX overhaul, 2026-09-27): five STAGES are the
// navigation, a few reference panes sit behind ⋯, and every old address or
// synonym resolves to a real pane — or says it could not (§17.1011 / §17.1114:
// `#/job/:id/assist` once rendered Overview silently; a retired hash route once
// rendered the dashboard instead of erroring and a dead link survived weeks).
//
// job_hub.js pulls the whole view tree, which touches `document` at import —
// stub just enough for a module-load, since the resolvers under test are pure.
import { test } from "node:test";
import assert from "node:assert/strict";

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
globalThis.window = { location: { hash: "" }, addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { TAB_ALIASES, resolveTab, KNOWN_TABS, unknownTabNotice, STAGES, stageFor, stageAccess, jobHref } = await import("../../app/ui/static/views/job_hub.js");

test("the stages are the flow, in order", () => {
  assert.deepEqual(STAGES.map(([k]) => k), ["idea", "approve", "plan", "run", "output"]);
});

test("every alias resolves to a REAL pane, not another alias", () => {
  const real = new Set(KNOWN_TABS);
  for (const [from, to] of Object.entries(TAB_ALIASES)) {
    assert.ok(real.has(to), `alias ${from} -> ${to} is not a real pane`);
    assert.ok(!(to in TAB_ALIASES), `alias ${from} -> ${to} chains to another alias`);
    assert.ok(!real.has(from), `${from} is a real pane and must not be aliased`);
  }
});

test("old addresses land on the right pane; real panes pass through", () => {
  assert.equal(resolveTab("assist"), "run");
  assert.equal(resolveTab("walkthrough"), "run");
  assert.equal(resolveTab("results"), "output");
  assert.equal(resolveTab("dag"), "plan");
  assert.equal(resolveTab("overview"), "auto", "the old Overview address opens the job where it is");
  assert.equal(resolveTab("brief"), "details");
  for (const t of KNOWN_TABS) assert.equal(resolveTab(t), t);
  assert.equal(resolveTab(undefined), "auto");
  assert.equal(resolveTab(""), "auto");
});

// ── §17.1114 (ledger U-1) — an unknown pane says so instead of silently showing something
test("known panes and their aliases produce no notice", () => {
  for (const t of KNOWN_TABS) assert.equal(unknownTabNotice(t), "");
  for (const a of Object.keys(TAB_ALIASES)) assert.equal(unknownTabNotice(a), "");
  assert.equal(unknownTabNotice(undefined), "", "no pane segment at all is the job's own address");
});

test("an unknown pane names itself in the notice", () => {
  const n = unknownTabNotice("outputs");
  assert.match(n, /No stage named “outputs”/);
});

test("#/job/:id opens on the stage the job is AT", () => {
  const j = (status, extra = {}) => ({ id: "J", status, node_count: 5, ...extra });
  assert.equal(stageFor(j("refining")), "approve");
  assert.equal(stageFor(j("awaiting_confirmation")), "approve");
  assert.equal(stageFor(j("researching")), "plan");
  assert.equal(stageFor(j("planning")), "plan");
  assert.equal(stageFor(j("executing")), "plan");
  for (const s of ["running", "assisted_running", "assisted_paused", "awaiting_assist", "blocked", "failed"]) assert.equal(stageFor(j(s)), "run", s);
  assert.equal(stageFor(j("completed")), "output");
  assert.equal(stageFor(j("cancelled", { node_count: 0 })), "idea");
  assert.equal(stageFor(j("cancelled")), "plan");
});

test("stages the job has not reached are locked unless there is something there", () => {
  const gate = stageAccess({ id: "J", status: "awaiting_confirmation", node_count: 0 });
  assert.deepEqual(gate, { idea: true, approve: true, plan: false, run: false, output: false });
  const planned = stageAccess({ id: "J", status: "executing", node_count: 12 });
  assert.deepEqual(planned, { idea: true, approve: true, plan: true, run: true, output: false });
  const done = stageAccess({ id: "J", status: "completed", node_count: 12, has_compiled_output: true });
  assert.ok(Object.values(done).every(Boolean));
  const walking = stageAccess({ id: "J", status: "assisted_running", node_count: 12, has_compiled_output: true });
  assert.equal(walking.output, true, "a compiled plan document is readable mid-walkthrough");
});

test("jobHref: the bare address is the job's current stage", () => {
  assert.equal(jobHref("J", "auto"), "#/job/J");
  assert.equal(jobHref("J"), "#/job/J");
  assert.equal(jobHref("J", "plan"), "#/job/J/plan");
});
