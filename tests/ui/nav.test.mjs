// UI design phase — grouped-nav integrity tests (node:test; dev-only).
// nav.js is the single source for the sidebar AND the command palette, so a
// malformed entry here silently breaks discoverability in both.
// Run: make test-ui   (or: node --test tests/ui/)
import { test } from "node:test";
import assert from "node:assert/strict";
import { NAV, NAV_GROUPS } from "../../app/ui/static/nav.js";

test("every group has a label and at least one item", () => {
  assert.ok(NAV_GROUPS.length >= 3);
  for (const g of NAV_GROUPS) {
    assert.ok(typeof g.label === "string" && g.label.length > 0);
    assert.ok(Array.isArray(g.items) && g.items.length > 0, `group ${g.label} empty`);
  }
});

test("flat NAV is exactly the groups flattened, in order", () => {
  assert.deepEqual(NAV, NAV_GROUPS.flatMap((g) => g.items));
});

test("ids and paths are unique; every entry is well-formed", () => {
  const ids = new Set();
  const paths = new Set();
  for (const n of NAV) {
    assert.ok(n.id && n.path && n.label && n.icon, `malformed entry ${JSON.stringify(n)}`);
    assert.ok(n.path.startsWith("/"), `path ${n.path} must start with /`);
    assert.ok(!ids.has(n.id), `duplicate id ${n.id}`);
    assert.ok(!paths.has(n.path), `duplicate path ${n.path}`);
    ids.add(n.id);
    paths.add(n.path);
  }
});

test("core destinations exist (easy access to all components)", () => {
  const ids = new Set(NAV.map((n) => n.id));
  // §17.859 — dag/theater/output collapsed into the job hub's tabs
  // (#/job/:id); they are deliberately ABSENT from the nav.
  for (const id of [
    "new", "chat", "dashboard", "approvals",
    "compare", "research", "rag", "library", "assist", "schedules",
    "models", "costs", "traces", "alerts", "settings", "setup",
  ]) {
    assert.ok(ids.has(id), `missing nav entry: ${id}`);
  }
  for (const id of ["dag", "theater", "output"]) {
    assert.ok(!ids.has(id), `${id} must stay retired from the nav (job hub owns it, §17.859)`);
  }
});

test("admin-only surfaces keep their flags (§17.810/815/816/817)", () => {
  const byId = Object.fromEntries(NAV.map((n) => [n.id, n]));
  for (const id of ["chat", "models", "traces", "alerts", "settings", "setup"]) {
    assert.equal(byId[id].adminOnly, true, `${id} must be adminOnly`);
  }
  // …and the everyday surfaces must NOT be admin-gated.
  for (const id of ["new", "dashboard", "approvals", "compare", "research", "rag", "assist"]) {
    assert.ok(!byId[id].adminOnly, `${id} must not be adminOnly`);
  }
});
