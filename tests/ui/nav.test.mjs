// UX overhaul (2026-09-27) — the navigation model: three PLACES on the rail,
// every page (places + tabs) in PAGES for the palette, and a place for every
// route head so the rail highlight never falls back silently.
import { test } from "node:test";
import assert from "node:assert/strict";
import { NAV, NAV_GROUPS, PAGES, PLACE_OF, placeOf } from "../../app/ui/static/nav.js";

test("the rail is exactly three places: Home, Knowledge, Settings", () => {
  assert.deepEqual(NAV.map((n) => n.id), ["home", "knowledge", "settings"]);
  assert.deepEqual(NAV, NAV_GROUPS.flatMap((g) => g.items));
  for (const n of NAV) assert.ok(!n.adminOnly, `${n.id} must not be admin-gated — a place is for everyone`);
});

test("ids and paths are unique; every entry is well-formed", () => {
  const ids = new Set();
  const paths = new Set();
  for (const n of PAGES) {
    assert.ok(n.id && n.path && n.label && n.icon, `malformed entry ${JSON.stringify(n)}`);
    assert.ok(n.path.startsWith("/"), `path ${n.path} must start with /`);
    assert.ok(!ids.has(n.id), `duplicate id ${n.id}`);
    assert.ok(!paths.has(n.path), `duplicate path ${n.path}`);
    ids.add(n.id);
    paths.add(n.path);
  }
  for (const n of NAV) assert.ok(ids.has(n.id), `${n.id} must be in PAGES too`);
});

test("every former destination is still reachable as a page (palette)", () => {
  const paths = new Set(PAGES.map((n) => n.path));
  for (const p of ["/new", "/knowledge/search", "/knowledge/research", "/knowledge/library", "/knowledge/schedules",
                   "/settings/models", "/settings/capabilities", "/settings/status", "/settings/costs",
                   "/settings/traces", "/settings/alerts", "/settings/config", "/settings/preferences", "/setup"]) {
    assert.ok(paths.has(p), `missing page: ${p}`);
  }
  // the retired standalone destinations must NOT come back as pages
  for (const id of ["dashboard", "jobs", "approvals", "assist", "compare", "dag", "theater", "output", "chat"]) {
    assert.ok(![...PAGES].some((n) => n.id === id), `${id} must stay retired — it is a Home filter or a job stage now`);
  }
});

test("admin-only surfaces keep their flags (§17.810/815/816/817)", () => {
  const byId = Object.fromEntries(PAGES.map((n) => [n.id, n]));
  for (const id of ["settings-models", "settings-traces", "settings-alerts", "settings-config", "setup", "settings-capabilities"]) {
    assert.equal(byId[id].adminOnly, true, `${id} must be adminOnly`);
  }
  for (const id of ["new", "home", "knowledge", "settings", "settings-costs", "settings-status", "settings-preferences", "knowledge-search"]) {
    assert.ok(!byId[id].adminOnly, `${id} must not be adminOnly`);
  }
});

test("every route head has a place, and unknown heads fall to Home", () => {
  for (const head of ["", "new", "chat", "job", "jobs", "approvals", "assist", "compare"]) assert.equal(placeOf(`/${head}/x`), "home", head);
  for (const head of ["knowledge", "research", "rag", "library", "schedules"]) assert.equal(placeOf(`/${head}`), "knowledge", head);
  for (const head of ["settings", "models", "costs", "traces", "alerts", "capabilities", "setup"]) assert.equal(placeOf(`/${head}/tab`), "settings", head);
  assert.equal(placeOf("/"), "home");
  assert.equal(placeOf("/nope"), "home");
  assert.ok(Object.values(PLACE_OF).every((v) => NAV.some((n) => n.id === v)), "every place must be a rail item");
});
