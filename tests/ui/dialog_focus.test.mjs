// §17.1118 (ledger U-11) — the Tab trap's index arithmetic.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.document = { activeElement: null, contains: () => false, createElement: () => ({}), addEventListener() {}, body: {} };
globalThis.window = new EventTarget();

const { nextFocusIndex } = await import("../../app/ui/static/components.js");

test("Tab wraps forward, Shift+Tab wraps backward, empty traps stay put", () => {
  assert.equal(nextFocusIndex(0, 3, false), 1);
  assert.equal(nextFocusIndex(2, 3, false), 0);
  assert.equal(nextFocusIndex(0, 3, true), 2);
  assert.equal(nextFocusIndex(1, 3, true), 0);
  assert.equal(nextFocusIndex(-1, 3, false), 0, "focus outside the trap → first item");
  assert.equal(nextFocusIndex(-1, 3, true), 2, "… or the last, going backwards");
  assert.equal(nextFocusIndex(0, 0, false), -1);
});
