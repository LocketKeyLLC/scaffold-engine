// §17.1134 (ledger D-6) — next_actions become buttons: filter, label, target.
import { test } from "node:test";
import assert from "node:assert/strict";

const na = await import("../../app/ui/static/next_actions.js");

const JOB = "11111111-2222-4333-8444-555555555555";
const SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

test("the noise filter drops wait and keeps everything else", () => {
  const out = na.filterRenderable([{ action: "wait" }, { action: "resume" }, null, { action: "view_plan" }]);
  assert.deepEqual(out.map((a) => a.action), ["resume", "view_plan"]);
});

test("every registry action has a label; node-specific ones name the node", () => {
  assert.equal(na.actionLabel({ action: "retry_node", command: `/exec retry ${JOB} T3` }), "Retry T3");
  assert.equal(na.actionLabel({ action: "skip_node", command: `/skip ${JOB} N2` }), "Skip N2");
  assert.equal(na.actionLabel({ action: "view_output" }), "View output");
  assert.equal(na.actionLabel({ action: "some_new_verb" }), "some new verb");
});

test("targets: hub tabs for verbs that live there, id-in-path calls for the rest", () => {
  assert.deepEqual(na.actionTarget({ action: "view_plan" }, JOB), { kind: "nav", href: `#/job/${JOB}/plan` });
  assert.deepEqual(na.actionTarget({ action: "retry_node", command: `/exec retry ${JOB} T3`, endpoint: "/exec/retry" }, JOB), { kind: "nav", href: `#/job/${JOB}/run` });
  assert.deepEqual(na.actionTarget({ action: "next_step", endpoint: `/assist/${SID}/next` }, JOB), { kind: "nav", href: `#/assist/${SID}` });
  assert.deepEqual(na.actionTarget({ action: "resume", endpoint: `/jobs/${JOB}/resume` }, JOB), { kind: "call", method: "POST", endpoint: `/jobs/${JOB}/resume` });
  assert.deepEqual(na.actionTarget({ action: "resume", endpoint: `/assist/${SID}/resume` }, JOB), { kind: "call", method: "POST", endpoint: `/assist/${SID}/resume` });
  const del = na.actionTarget({ action: "delete", endpoint: `/jobs/${JOB}` }, JOB);
  assert.equal(del.kind, "call"); assert.equal(del.method, "DELETE"); assert.ok(del.confirm, "destructive actions confirm first");
});
