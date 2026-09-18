// §17.1093/§17.1097 — a staged plan-change proposal auto-opens the modal
// exactly ONCE (the first time it is seen, from any source), then only ever
// shows the chip. It must never re-open after the operator acts on it, and a
// background poll must never re-pop it.
import { test } from "node:test";
import assert from "node:assert/strict";

const { replanRenderDecision } = await import("../../app/ui/static/views/assist.js");
const SIG = '[["T37","repair","fix it"]]';

test("first sighting opens the modal (any source)", () => {
  const d = replanRenderDecision(SIG, { announced: new Set(), resolved: new Set(), snoozed: new Set() });
  assert.deepEqual([d.show, d.openModal, d.firstSighting], [true, true, true]);
});

test("once announced, later renders show the chip but never re-open", () => {
  const announced = new Set([SIG]);
  const d = replanRenderDecision(SIG, { announced, resolved: new Set(), snoozed: new Set() });
  assert.deepEqual([d.show, d.openModal], [true, false]);
});

test("an applied/discarded proposal never renders again", () => {
  const resolved = new Set([SIG]);
  for (const announced of [new Set(), new Set([SIG])]) {
    const d = replanRenderDecision(SIG, { announced, resolved, snoozed: new Set() });
    assert.equal(d.show, false);
  }
});

test("'Later' (snooze) suppresses the auto-open on first sighting but keeps the chip", () => {
  const snoozed = new Set([SIG]);
  const d = replanRenderDecision(SIG, { announced: new Set(), resolved: new Set(), snoozed });
  assert.deepEqual([d.show, d.openModal], [true, false]);
});

test("a DIFFERENT new proposal still opens even after another was announced", () => {
  const announced = new Set([SIG]);
  const other = '[["ADD9","revise","do X"]]';
  const d = replanRenderDecision(other, { announced, resolved: new Set(), snoozed: new Set() });
  assert.deepEqual([d.show, d.openModal], [true, true]);
});
