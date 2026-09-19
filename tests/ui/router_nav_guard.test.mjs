// §17.1116 (ledger U-6) — a view with work that dies on navigation can ask first.
import { test } from "node:test";
import assert from "node:assert/strict";

const { navAllowed } = await import("../../app/ui/static/router.js");
const { RESEARCH_LEAVE_MSG } = await import("../../app/ui/static/views/research.js").catch(() => ({ RESEARCH_LEAVE_MSG: null }));

test("no guard, or same path → always allowed, confirm never asked", () => {
  let asked = 0;
  const confirm = () => { asked++; return false; };
  assert.equal(navAllowed(null, "/research", "/jobs", confirm), true);
  assert.equal(navAllowed(() => "leave?", "/research", "/research", confirm), true);
  assert.equal(asked, 0);
});

test("a guard with nothing to lose → allowed without asking", () => {
  let asked = 0;
  assert.equal(navAllowed(() => null, "/research", "/jobs", () => { asked++; return false; }), true);
  assert.equal(asked, 0);
});

test("a guard with something to lose asks, and the answer decides", () => {
  const guard = () => "Research is running and will be cancelled. Leave?";
  const seen = [];
  assert.equal(navAllowed(guard, "/research", "/jobs", (m) => { seen.push(m); return false; }), false);
  assert.equal(navAllowed(guard, "/research", "/jobs", (m) => { seen.push(m); return true; }), true);
  assert.equal(seen.length, 2);
  assert.match(seen[0], /cancelled/);
});

test("the research view's message says what is lost (when importable here)", (t) => {
  if (!RESEARCH_LEAVE_MSG) { t.skip("research.js not importable without a DOM"); return; }
  assert.match(RESEARCH_LEAVE_MSG, /CANCELLED/);
});
