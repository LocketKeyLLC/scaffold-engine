// §17.1095 — a mid-turn engine restart must not alarm when the work completed,
// and must restore an unanswered message instead of "retype it."
import { test } from "node:test";
import assert from "node:assert/strict";

const { restartRecovery } = await import("../../app/ui/static/views/assist.js");
const R = "The engine restarted mid-turn — please resend your message.";

test("answered: the operator's last message already got a reply → reassure, no restore", () => {
  const turns = [{ id: 8, role: "operator", content: "qm config 106" }, { id: 9, role: "assistant", content: "here's the config" }];
  assert.deepEqual(restartRecovery(turns, R), { kind: "answered", text: "" });
});

test("restore: the last message was unanswered → hand its text back", () => {
  const turns = [{ id: 9, role: "assistant", content: "old answer" }, { id: 12, role: "operator", content: "48-line paste" }];
  assert.deepEqual(restartRecovery(turns, R), { kind: "restore", text: "48-line paste" });
});

test("the optimistic (_pending) copy is not treated as durable", () => {
  const turns = [{ id: 9, role: "assistant", content: "a" }, { role: "operator", content: "typed", _pending: true }];
  assert.equal(restartRecovery(turns, R).kind, "generic");   // no durable operator message yet
});

test("a non-restart error is left generic", () => {
  const turns = [{ id: 12, role: "operator", content: "x" }];
  assert.equal(restartRecovery(turns, "diagnosis failed: boom").kind, "generic");
});

test("no operator messages → generic", () => {
  assert.equal(restartRecovery([{ id: 1, role: "assistant", content: "hi" }], R).kind, "generic");
  assert.equal(restartRecovery([], R).kind, "generic");
});

test("the stalled-tail message is also recognised as a restart-class recovery", () => {
  const turns = [{ id: 5, role: "operator", content: "still here" }];
  assert.equal(restartRecovery(turns, "This turn stalled for more than 30 minutes and was closed — please resend your message.").kind, "restore");
});
