// §17.1093 — a staged plan-change proposal must not reappear after the operator
// acts on it, and a background poll must never re-open the modal.
import { test } from "node:test";
import assert from "node:assert/strict";

const { replanRenderDecision } = await import("../../app/ui/static/views/assist.js");
const SIG = '[["T37","repair","fix it"]]';

test("a fresh in-turn proposal opens the modal", () => {
  const d = replanRenderDecision(SIG, { open: true, background: false, isNew: true, resolved: new Set(), snoozed: new Set() });
  assert.deepEqual([d.show, d.openModal], [true, true]);
});

test("a background poll shows the chip but never force-opens", () => {
  const d = replanRenderDecision(SIG, { open: true, background: true, isNew: true, resolved: new Set(), snoozed: new Set() });
  assert.deepEqual([d.show, d.openModal], [true, false]);
});

test("an applied/discarded proposal never renders again", () => {
  const resolved = new Set([SIG]);
  for (const opts of [{ open: true, background: false }, { open: true, background: true }, { open: false }]) {
    const d = replanRenderDecision(SIG, { ...opts, isNew: true, resolved, snoozed: new Set() });
    assert.equal(d.show, false, JSON.stringify(opts));
  }
});

test("'Later' stops the modal re-opening but keeps the chip", () => {
  const snoozed = new Set([SIG]);
  const d = replanRenderDecision(SIG, { open: true, background: false, isNew: true, resolved: new Set(), snoozed });
  assert.deepEqual([d.show, d.openModal], [true, false]);
});

test("the same proposal seen again in one view does not re-pop (isNew false)", () => {
  const d = replanRenderDecision(SIG, { open: true, background: false, isNew: false, resolved: new Set(), snoozed: new Set() });
  assert.deepEqual([d.show, d.openModal], [true, false]);
});
