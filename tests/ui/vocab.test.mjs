// UX overhaul (2026-09-27) — the operator vocabulary: every job status the
// engine can write (app/modules/job_state.py JOB_STATUSES) has plain words and
// exactly ONE next step; no raw enum ever reaches a label.
import { test } from "node:test";
import assert from "node:assert/strict";
import { STATUS_LABEL, statusLabel, nextStep, bucketOf, NEEDS_YOU, IN_PROGRESS, DONE, deliverableLabel } from "../../app/ui/static/vocab.js";

// Mirror of job_state.JOB_STATUSES (tests/test_job_state.py pins the Python side).
const JOB_STATUSES = [
  "pending", "refining", "awaiting_confirmation", "researching", "planning", "executing", "running",
  "completed", "failed", "cancelled", "blocked", "assisted_executing", "assisted_running", "assisted_paused",
  "aggregating", "awaiting_assist",
];

test("every job status has plain words — no underscores, no enum leaks", () => {
  for (const s of JOB_STATUSES) {
    assert.ok(STATUS_LABEL[s], `no label for ${s}`);
    assert.ok(!/_/.test(STATUS_LABEL[s]), `${s} → ${STATUS_LABEL[s]} still looks like an enum`);
    if (s.includes("_")) assert.notEqual(STATUS_LABEL[s].toLowerCase(), s.replace(/_/g, " "), `${s} label is just the enum with spaces`);
  }
  assert.equal(statusLabel("assisted_running"), "In progress");
  assert.equal(statusLabel("some_new_status"), "some new status", "an unknown status degrades to words, never crashes");
  assert.equal(statusLabel(""), "Unknown");
});

test("every job status is in exactly one Home bucket", () => {
  for (const s of JOB_STATUSES) {
    const n = [NEEDS_YOU, IN_PROGRESS, DONE].filter((set) => set.has(s)).length;
    assert.equal(n, 1, `${s} is in ${n} buckets`);
  }
  assert.equal(bucketOf("awaiting_confirmation"), "needs_you");
  assert.equal(bucketOf("assisted_running"), "in_progress");
  assert.equal(bucketOf("completed"), "done");
});

test("every job status maps to one verb and one job-page destination", () => {
  for (const s of JOB_STATUSES) {
    const step = nextStep({ id: "J1", status: s, node_count: 3 });
    assert.ok(step.label && step.href, `${s} has no next step`);
    assert.match(step.href, /^#\/job\/J1(\/(plan|run|output))?$/, `${s} → ${step.href} is not a job-page stage`);
    assert.equal(typeof step.primary, "boolean");
  }
  // the ones the operator opens the app for are PRIMARY (acts), the rest are looks
  for (const s of ["awaiting_confirmation", "awaiting_assist", "assisted_running", "assisted_paused", "blocked", "failed"]) {
    assert.equal(nextStep({ id: "J1", status: s, node_count: 3 }).primary, true, s);
  }
  for (const s of ["refining", "researching", "planning", "running", "completed"]) {
    assert.equal(nextStep({ id: "J1", status: s, node_count: 3 }).primary, false, s);
  }
});

test("a walkthrough row names the step position when progress is known", () => {
  const step = nextStep({ id: "J1", status: "assisted_running", node_count: 131 }, { progress: { completed: 108, total: 131 } });
  assert.equal(step.label, "Continue");
  assert.match(step.hint, /step 109 of 131/);
  const last = nextStep({ id: "J1", status: "assisted_running" }, { progress: { completed: 131, total: 131 } });
  assert.match(last.hint, /step 131 of 131/, "never claims a step beyond the plan");
});

test("deliverable kinds read as words", () => {
  assert.equal(deliverableLabel("plan_only"), "Plan (not executed)");
  assert.equal(deliverableLabel("weird_kind"), "weird kind");
  assert.equal(deliverableLabel(""), "");
});
