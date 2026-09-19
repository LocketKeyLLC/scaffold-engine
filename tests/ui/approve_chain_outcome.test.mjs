// §17.1113 (Phase 1 ledger U-2) — the approve chain's outcome is typed and a
// non-answer is never "approved".
import { test } from "node:test";
import assert from "node:assert/strict";

const { chainOutcome, chainFailureText, CHAIN_UNREACHABLE_AFTER, CHAIN_TIMEOUT_MS } =
  await import("../../app/ui/static/views/approvals.js");

test("done and error are terminal; running phases are not", () => {
  assert.deepEqual(chainOutcome({ chain: "done", phase: "execute" }), { chain: "done", phase: "execute" });
  assert.deepEqual(chainOutcome({ chain: "error", error: "dag failed" }), { chain: "error", error: "dag failed" });
  assert.equal(chainOutcome({ chain: "running", phase: "research" }), null);
  assert.equal(chainOutcome({ chain: "idle", node_count: 0 }), null, "idle with no plan yet is still waiting");
  assert.deepEqual(chainOutcome({ chain: "idle", node_count: 4, phase: "planning" }), { chain: "done", phase: "planning" });
});

test("a missing or malformed state is NOT an outcome (the old null-as-success bug)", () => {
  assert.equal(chainOutcome(null), null);
  assert.equal(chainOutcome(undefined), null);
  assert.equal(chainOutcome("done"), null);
});

test("failure text never says approved and names what is known", () => {
  for (const out of [{ chain: "error", error: "boom" }, { chain: "unreachable", error: "Failed to fetch" },
                     { chain: "timeout" }, null]) {
    const t = chainFailureText(out);
    assert.ok(!/approved/i.test(t), t);
    assert.ok(t.length > 20);
  }
  assert.match(chainFailureText({ chain: "error", error: "boom" }), /boom/);
  assert.match(chainFailureText({ chain: "unreachable", error: "Failed to fetch" }), /Lost contact.*Failed to fetch/);
  assert.match(chainFailureText({ chain: "timeout" }), /hour/);
});

test("the unreachable threshold and the timeout are bounded", () => {
  assert.ok(CHAIN_UNREACHABLE_AFTER >= 3 && CHAIN_UNREACHABLE_AFTER <= 60);
  assert.equal(CHAIN_TIMEOUT_MS, 60 * 60 * 1000);
});
