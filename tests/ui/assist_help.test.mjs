// §17.1096 — the "?" help panel is built from one source (ASSIST_HELP) and
// every control the operator can press has a plain-language explanation there.
import { test } from "node:test";
import assert from "node:assert/strict";

const { ASSIST_HELP, helpSections, helpControlLabels } = await import("../../app/ui/static/views/assist.js");

test("helpSections yields the buttons and the behaviours, each with plain text", () => {
  const secs = helpSections();
  assert.equal(secs.length, 2);
  const [controls, behaviors] = secs;
  assert.match(controls.heading, /button/i);
  assert.match(behaviors.heading, /on its own/i);
  for (const s of secs) {
    assert.ok(s.items.length > 0, `${s.heading} has no items`);
    for (const it of s.items) {
      assert.ok(it.term && it.term.length, "an item has no term");
      assert.ok(it.plain && it.plain.length > 20, `"${it.term}" has no real explanation`);
    }
  }
});

test("every control label is explained (the wiring the '?' panel depends on)", () => {
  const labels = helpControlLabels();
  assert.ok(labels.includes("✓ Done → next step"));
  assert.ok(labels.includes("🩺 Verify state"));
  for (const l of labels) assert.ok(ASSIST_HELP.controls[l], `${l} missing help text`);
});

test("the behaviours cover what had no UI explanation", () => {
  const titles = ASSIST_HELP.behaviors.map((b) => b.title.toLowerCase()).join(" | ");
  for (const needle of ["restart", "status line", "proposal", "invent"]) {
    assert.ok(titles.includes(needle), `no behaviour about ${needle}: ${titles}`);
  }
});
