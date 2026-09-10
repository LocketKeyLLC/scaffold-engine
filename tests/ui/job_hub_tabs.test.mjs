// §17.1011 — job-hub tab aliases.
//
// `#/job/:id/assist` rendered Overview silently: assist actually lives in the
// Run tab, and an unknown tab fell through to the default. Same shape as
// §17.859, where a retired hash route rendered the dashboard instead of
// erroring and the idea→approve link stayed dead for weeks.
//
// job_hub.js pulls the whole view tree, which touches `document` at import —
// stub just enough for a module-load, since the resolver under test is pure.
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
globalThis.window = { location: { hash: "" }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

// ── §17.1011 — tab aliases ──────────────────────────────────────────────
// `#/job/:id/assist` rendered Overview silently (assist actually lives in the
// Run tab). Same shape as §17.859, where a retired hash route rendered the
// dashboard instead of erroring and a dead link survived for weeks.
const { TAB_ALIASES, resolveTab } = await import("../../app/ui/static/views/job_hub.js");

test("every alias resolves to a REAL tab, not another alias", () => {
  const real = new Set(["overview", "plan", "run", "output", "traces", "costs"]);
  for (const [from, to] of Object.entries(TAB_ALIASES)) {
    assert.ok(real.has(to), `alias ${from} -> ${to} is not a real tab`);
    assert.ok(!(to in TAB_ALIASES), `alias ${from} -> ${to} chains to another alias`);
    assert.ok(!real.has(from), `${from} is a real tab and must not be aliased`);
  }
});

test("assist resolves to the Run tab, and real tabs pass through", () => {
  assert.equal(resolveTab("assist"), "run");
  assert.equal(resolveTab("walkthrough"), "run");
  assert.equal(resolveTab("results"), "output");
  for (const t of ["overview", "plan", "run", "output", "traces", "costs"]) {
    assert.equal(resolveTab(t), t);
  }
  assert.equal(resolveTab(undefined), "overview");
  assert.equal(resolveTab(""), "overview");
});
