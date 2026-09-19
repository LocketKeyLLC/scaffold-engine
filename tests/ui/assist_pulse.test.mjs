// §17.1117 (ledger U-8) — the liveness dot's state thresholds are the ones the help panel quotes.
import { test } from "node:test";
import assert from "node:assert/strict";

const { pulseState, PULSE_QUIET_MS, PULSE_STALE_MS, ASSIST_HELP } = await import("../../app/ui/static/views/assist.js");

test("live → quiet at 45 s → stale at 2 min", () => {
  assert.equal(pulseState(0), "live");
  assert.equal(pulseState(PULSE_QUIET_MS - 1), "live");
  assert.equal(pulseState(PULSE_QUIET_MS), "quiet");
  assert.equal(pulseState(PULSE_STALE_MS - 1), "quiet");
  assert.equal(pulseState(PULSE_STALE_MS), "stale");
  assert.equal(pulseState(-5), "live");
});

test("the help panel quotes the same thresholds", () => {
  const entry = ASSIST_HELP.behaviors.find((b) => b.title === "The status line above the box");
  assert.ok(entry);
  assert.match(entry.plain, /45 seconds/);
  assert.match(entry.plain, /two minutes/);
  assert.equal(PULSE_QUIET_MS, 45000); assert.equal(PULSE_STALE_MS, 120000);
});
