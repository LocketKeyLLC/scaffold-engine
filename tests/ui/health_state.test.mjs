// §17.1116 (ledger U-5) — the reachability state machine has memory.
import { test } from "node:test";
import assert from "node:assert/strict";

const { INITIAL, nextHealth, healthText, dotState, pillVisible } = await import("../../app/ui/static/health_state.js");

test("up → unreachable keeps lastOkAt and stamps since once", () => {
  let st = nextHealth(INITIAL, { ok: true, status: "healthy" }, 1_000_000);
  assert.equal(st.state, "up"); assert.equal(st.lastOkAt, 1_000_000); assert.equal(st.failures, 0);
  st = nextHealth(st, { ok: false, error: "Failed to fetch" }, 1_015_000);
  assert.equal(st.state, "unreachable"); assert.equal(st.since, 1_015_000); assert.equal(st.lastOkAt, 1_000_000);
  st = nextHealth(st, { ok: false, error: "Failed to fetch" }, 1_030_000);
  assert.equal(st.since, 1_015_000, "since is the FIRST failure, not the latest");
  assert.equal(st.failures, 2);
  const txt = healthText(st, 1_180_000);
  assert.match(txt, /engine unreachable since \d\d:\d\d · last seen 3 min ago/);
  assert.equal(dotState(st), "down"); assert.equal(pillVisible(st), true);
});

test("recovery resets failures and stamps a new since", () => {
  let st = nextHealth(INITIAL, { ok: false, error: "x" }, 10);
  st = nextHealth(st, { ok: true, status: "ok" }, 20);
  assert.deepEqual([st.state, st.failures, st.since, st.lastOkAt], ["up", 0, 20, 20]);
  assert.equal(healthText(st), "orchestrator up");
  assert.equal(pillVisible(st), false, "the pill is quiet when healthy");
  assert.equal(dotState(st), "up");
});

test("a non-healthy status is degraded, visible, and names the status", () => {
  const st = nextHealth(INITIAL, { ok: true, status: "degraded" }, 5);
  assert.equal(st.state, "degraded"); assert.equal(st.lastOkAt, 5, "the engine answered → reachable");
  assert.equal(healthText(st), "orchestrator degraded (degraded)");
  assert.equal(pillVisible(st), true); assert.equal(dotState(st), "degraded");
});

test("the initial state is checking, hidden pill, unknown dot", () => {
  assert.equal(healthText(INITIAL), "checking…"); assert.equal(pillVisible(INITIAL), false); assert.equal(dotState(INITIAL), "unknown");
});
