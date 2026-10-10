// §17.1452 — the composer says what the engine is doing. Live (ADD128, 2026-10-10): after a correction
// cleared the step's walkthrough, the turn ended on a note and the page could not be told apart from a hung
// one ("there is no indicator on the web ui as to if the engine is still running").
import { test } from "node:test";
import assert from "node:assert/strict";

const { engineState } = await import("../../app/ui/static/views/assist.js");

const ADD128_AFTER_NOTE = [
  { role: "assistant", kind: "fix", node_key: "ADD128", content: "## 👉 Do this next…" },
  { role: "operator", kind: "note", node_key: "ADD128", content: "caddy (ct 120) is at 192.168.1.127, not 192.168.1.26." },
  { role: "assistant", kind: "note", node_key: "ADD128", content: "🔁 **Plan updated from your note** …" },
];

test("while a turn runs it says so", () => {
  assert.equal(engineState({ guiding: true, turns: ADD128_AFTER_NOTE, stepKey: "ADD128" }).state, "working");
});

test("idle after a note on the step: no walkthrough on screen, the button comes back", () => {
  const st = engineState({ guiding: false, turns: ADD128_AFTER_NOTE, stepKey: "ADD128" });
  assert.equal(st.state, "idle");
  assert.equal(st.needsGuide, true);
  assert.match(st.label, /no walkthrough on screen for ADD128/);
});

test("idle with the step's walkthrough last: just waiting", () => {
  const turns = [...ADD128_AFTER_NOTE, { role: "assistant", kind: "guide", node_key: "ADD128", content: "## 👉 Do this next" }];
  const st = engineState({ guiding: false, turns, stepKey: "ADD128" });
  assert.equal(st.needsGuide, false);
  assert.equal(st.label, "○ Engine idle — waiting for you");
});

test("another step's walkthrough does not count for this one", () => {
  const turns = [{ role: "assistant", kind: "guide", node_key: "ADD49", content: "qm status 110" }];
  assert.equal(engineState({ guiding: false, turns, stepKey: "ADD128" }).needsGuide, true);
});
