// UX overhaul (2026-09-27) — Home rows: the filter model and the row model are pure.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { _m: new Map(), getItem(k) { return this._m.get(k) ?? null; }, setItem(k, v) { this._m.set(k, v); }, removeItem(k) { this._m.delete(k); } };
const noopEl = () => ({ appendChild() {}, append() {}, remove() {}, setAttribute() {}, addEventListener() {}, removeEventListener() {},
  querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {} }, style: {}, dataset: {}, children: [], insertBefore() {} });
globalThis.document = { createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, body: noopEl(), documentElement: noopEl() };
globalThis.window = { location: { hash: "" }, addEventListener() {}, removeEventListener() {}, dispatchEvent() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { FILTERS, FILTER_ALIASES, resolveFilter, matchesFilter, rowModel } = await import("../../app/ui/static/views/home.js");

test("old #/jobs/:filter addresses resolve to the four chips", () => {
  const keys = FILTERS.map(([k]) => k);
  assert.deepEqual(keys, ["active", "needs_you", "done", "all"]);
  for (const [from, to] of Object.entries(FILTER_ALIASES)) assert.ok(keys.includes(to), `${from} → ${to} is not a chip`);
  assert.equal(resolveFilter("attention"), "needs_you");
  assert.equal(resolveFilter("completed"), "done");
  assert.equal(resolveFilter(undefined), "active");
  assert.equal(resolveFilter("garbage"), "active");
});

test("Active is everything that is not history; Needs you is the operator's queue", () => {
  const j = (status) => ({ id: "x", status });
  assert.equal(matchesFilter(j("assisted_running"), "active"), true);
  assert.equal(matchesFilter(j("awaiting_confirmation"), "active"), true);
  assert.equal(matchesFilter(j("completed"), "active"), false);
  assert.equal(matchesFilter(j("awaiting_confirmation"), "needs_you"), true);
  assert.equal(matchesFilter(j("assisted_running"), "needs_you"), false);
  assert.equal(matchesFilter(j("cancelled"), "done"), true);
  assert.equal(matchesFilter(j("cancelled"), "all"), true);
});

test("the row model reads the summary fields the inventory requires and names one action", () => {
  const job = { id: "J1", title: "Home lab", status: "assisted_running", node_count: 131,
    created_at: "2026-08-27T20:33:53Z", updated_at: "2026-09-22T22:51:15Z", completed_at: null, parent_job_id: "U1", component_index: 1 };
  const work = { id: "J1", next_actions: [{ action: "next_step", endpoint: "/assist/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/next" }] };
  const m = rowModel(job, { work, progress: { completed: 108, total: 131 } });
  assert.equal(m.action.label, "Continue");
  assert.equal(m.action.href, "#/job/J1/run");
  assert.equal(m.sessionId, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
  assert.match(m.hint, /step 109 of 131/);
  assert.equal(m.part, "part 2 of a larger build");
  assert.equal(m.when, "2026-09-22T22:51:15Z");
  const done = rowModel({ id: "J2", status: "completed", completed_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-02T00:00:00Z" });
  assert.equal(done.when, "2026-09-01T00:00:00Z", "a finished job is dated by when it finished");
  assert.equal(done.action.label, "Read output");
});
