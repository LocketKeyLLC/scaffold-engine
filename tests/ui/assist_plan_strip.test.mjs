// §17.1055 — the plan strip: where am I, framed by the step just finished
// and the one coming. Pure helper, tested against the live /steps shape.
import { test } from "node:test";
import assert from "node:assert/strict";

const { planPosition } = await import("../../app/ui/static/views/assist.js");

const STEPS = [
  { node_key: "T1", title: "Audit", step_status: "committed" },
  { node_key: "T2", title: "Decide GPU", step_status: "committed" },
  { node_key: "ADD7", title: "Delete VMs", step_status: "skipped" },
  { node_key: "ADD12", title: "Fix the NVIDIA driver", step_status: "committed" },
  { node_key: "ADD13", title: "Fix the control-panel backend", step_status: "presented" },
  { node_key: "ADD14", title: "Repair the Caddyfile", step_status: "pending" },
  { node_key: "T37", title: "Validate entire build", step_status: "pending" },
];

test("position, done count, and the finished/coming neighbours", () => {
  const p = planPosition(STEPS, "ADD13");
  assert.equal(p.index, 5);
  assert.equal(p.total, 7);
  assert.equal(p.done, 4);           // committed ×3 + skipped
  assert.equal(p.prev.node_key, "ADD12");
  assert.equal(p.next.node_key, "ADD14");
});

test("skipped steps count as finished for 'previous'; unfinished ones are skipped over for 'next'", () => {
  const steps = STEPS.map((s) => s.node_key === "ADD12" ? { ...s, step_status: "pending" } : s);
  const p = planPosition(steps, "ADD13");
  assert.equal(p.prev.node_key, "ADD7");   // nearest FINISHED before, not nearest row
  const last = planPosition(STEPS, "T37");
  assert.equal(last.next, null);
  assert.equal(last.index, 7);
});

test("no current step → index 0, neighbours null, counts still right", () => {
  const p = planPosition(STEPS, null);
  assert.equal(p.index, 0);
  assert.equal(p.prev, null);
  assert.equal(p.next, null);
  assert.equal(p.done, 4);
  assert.deepEqual(planPosition(undefined, "x"), { index: 0, total: 0, done: 0, prev: null, next: null });
});
