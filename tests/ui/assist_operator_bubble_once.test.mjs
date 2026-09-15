// §17.1082 — an operator message is painted ONCE while its turn runs, and
// once after the server's copy comes back — never twice.
import { test } from "node:test";
import assert from "node:assert/strict";

const { transcriptPlan } = await import("../../app/ui/static/views/assist.js");

const count = (plan, text) =>
  plan.durable.filter((t) => t.role === "operator" && t.content === text).length +
  plan.pending.filter((o) => o.content === text).length;

test("mid-turn: the optimistic copy in turns is not painted; pendingOps paints it once", () => {
  const sent = { role: "operator", kind: "message", content: "what is the next step?", _pending: true };
  const turns = [{ id: 1, role: "assistant", kind: "guide", content: "## Do this" }, sent];
  const plan = transcriptPlan(turns, [sent]);
  assert.equal(plan.durable.length, 1);
  assert.equal(count(plan, "what is the next step?"), 1);
});

test("after reload: the durable copy paints and the pending entry retires", () => {
  const sent = { role: "operator", kind: "message", content: "what is the next step?", _pending: true };
  const turns = [{ id: 1, role: "assistant", kind: "guide", content: "## Do this" },
                 { id: 2, role: "operator", kind: "message", content: "what is the next step?" }];
  const plan = transcriptPlan(turns, [sent]);
  assert.equal(plan.pending.length, 0);
  assert.equal(count(plan, "what is the next step?"), 1);
});

test("a different pending message survives the retire filter", () => {
  const plan = transcriptPlan([{ id: 2, role: "operator", content: "a" }], [{ content: "b" }]);
  assert.deepEqual(plan.pending, [{ content: "b" }]);
});
