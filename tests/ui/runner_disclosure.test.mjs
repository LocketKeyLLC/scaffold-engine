// §17.1176 — the console must not tell the operator the engine cannot reach
// their machine while a local runner is wired to it.
//
// The audit of 2026-09-25 found the claim in three surfaces — the assist
// contract card, ASSIST_HELP's "🤝 Engine does it", and the plan guidance —
// while an enabled `pve-runner` row plus `mcp_tool_enabled` meant
// `assist_turn._auto_lookup` ran model-authored commands on the operator's
// Proxmox host automatically, twice per turn, with no confirmation. Nothing in
// the UI said so, and nothing offered a way to stop it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runnerNote, ASSIST_HELP } from "../../app/ui/static/views/assist.js";

test("with no runner, the absolute claim is the one that is true", () => {
  const n = runnerNote({ connected: false });
  assert.match(n.lede, /never touches your machine/);
  assert.doesNotMatch(n.lede, /read-only checks/i);
});

test("with a runner, the copy says what the engine actually does", () => {
  const n = runnerNote({ connected: true, name: "pve-runner" });
  assert.doesNotMatch(n.lede, /never touches your machine/,
    "the reassuring absolute must not survive a wired runner");
  assert.match(n.lede, /READ-ONLY/);
  assert.match(n.lede, /pve-runner/);
  assert.match(n.lede, /never run anything that writes/i);
  assert.match(n.step2, /local runner does for you/);
});

test("the default is the CONSERVATIVE reading, never the reassuring one", () => {
  // Until `GET /setup/runner` answers we know nothing; the claim we must never
  // make by accident is "it cannot reach your machine".
  for (const state of [undefined, null, {}, { connected: undefined }]) {
    assert.doesNotMatch(runnerNote(state).lede, /READ-ONLY/,
      "an unknown runner state must not imply one is connected");
  }
});

test("the help panel explains the runner rather than denying it", () => {
  const behaviours = ASSIST_HELP.behaviors.map((b) => b.title + " " + b.plain).join("\n");
  assert.match(behaviours, /local runner/i,
    "six behaviours described the engine's autonomy and none mentioned the runner");
  assert.match(behaviours, /read-only/i);
  assert.match(behaviours, /\[local-runner\]/, "the transcript marker is how they see it happen");
  assert.match(behaviours, /disconnect|Capabilities/i, "and a way to switch it off");
  assert.doesNotMatch(ASSIST_HELP.controls["🤝 Engine does it"], /never touches your machine/);
});

test("no surface asserts the absolute unconditionally any more", () => {
  for (const f of ["app/ui/static/views/plan.js"]) {
    const src = readFileSync(f, "utf8");
    // the phrase may appear in a §-comment explaining the fix, never in copy
    for (const line of src.split("\n")) {
      if (/never touch(es)? your machine/i.test(line)) {
        assert.match(line.trim(), /^\/\//, `${f}: operator-facing copy still asserts it: ${line.trim()}`);
      }
    }
  }
});
