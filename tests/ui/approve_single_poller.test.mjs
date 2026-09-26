// §17.1180 (audit U4 + U5) — one poller, one writer, one timer.
//
// Before: `pollStatus` ran on its own 2.5 s setInterval for the whole approve
// chain while `waitForChain` polled at 2.5 s too — ~2,880 requests per open tab
// per hour (CHAIN_TIMEOUT_MS is one hour) against a single-worker uvicorn, for
// ONE approval. Both wrote `.progress-msg`, so the line the operator read was
// whichever ticked last. And `approve()` assigned its interval to the SAME
// `pollTimer` the 4 s wait poll used, without clearing it — orphaning an
// interval that then fired with nothing able to cancel it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = new URL("../../app/ui/static/views/approvals.js", import.meta.url);
const raw = readFileSync(SRC, "utf8");
// Scan CODE, not prose: the comments in this file explain the defect by name,
// so a substring search trips on its own rationale (the same way an earlier
// version of this test did).
const src = raw
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .split("\n").filter((ln) => !/^\s*\/\//.test(ln)).join("\n");
const { progressLine, CHAIN_POLL_MS } = await import("../../app/ui/static/views/approvals.js");

test("one poll's response carries everything both lines were built from", () => {
  assert.equal(progressLine({ phase: "research" }), "Researching & compiling…");
  assert.equal(progressLine({ phase: "planning", node_count: 4 }),
    "Generating plan (DAG)… · 4 nodes planned");
  // no phase yet → fall back to the raw status, which is what pollStatus showed
  assert.equal(progressLine({ status: "researching" }), "researching…");
  assert.equal(progressLine({ status: "planning", node_count: 7 }),
    "planning… · 7 nodes planned");
});

test("an empty state produces no line rather than a stray separator", () => {
  assert.equal(progressLine({}), "");
  assert.equal(progressLine({ node_count: 0 }), "");
});

test("the second poller is gone", () => {
  assert.ok(!/setInterval\(\s*pollStatus/.test(src),
    "approve() still starts a second interval");
  assert.ok(!/async function pollStatus\b/.test(src),
    "pollStatus still exists as a separate poller");
});

test("pollTimer has exactly one owner — the wait poll", () => {
  const assignments = [...src.matchAll(/pollTimer\s*=\s*setInterval/g)];
  assert.equal(assignments.length, 1,
    `pollTimer is assigned an interval ${assignments.length} times; one variable ` +
    "serving two polls is what orphaned the first one");
});

test("approve() stops the wait poll instead of overwriting it", () => {
  const body = src.slice(src.indexOf("async function approve"));
  const upTo = body.slice(0, body.indexOf("await api.post"));
  assert.ok(/stopWaitPoll\(\)/.test(upTo),
    "approve() must clear the wait poll before running the chain");
});

test("only one poller WRITES the progress line", () => {
  // Creating the element (`el("span", { class: "progress-msg" })`) is not a
  // write; reading it back to retarget its text is. Both remaining readers are
  // inside waitForChain — its success path and its unreachable path.
  const writers = [...src.matchAll(/querySelector\("\.progress-msg"\)/g)];
  assert.ok(writers.length <= 2,
    `${writers.length} sites retarget .progress-msg — two pollers fighting over ` +
    "one line is the defect this closed");
  const waitBody = src.slice(src.indexOf("async function waitForChain"));
  const inWait = [...waitBody.matchAll(/querySelector\("\.progress-msg"\)/g)];
  assert.equal(inWait.length, writers.length,
    "every write to the progress line must come from the single poller");
});

test("the poll interval is unchanged — this halved the count, not the cadence", () => {
  assert.equal(CHAIN_POLL_MS, 2500);
});
