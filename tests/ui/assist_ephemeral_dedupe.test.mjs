// §17.1054 — the streamed answer retires against the durable transcript by
// id watermark, never by comparing the browser clock to the server's.
import { test } from "node:test";
import assert from "node:assert/strict";

const { maxTurnId, ephemeralIsDurable } =
  await import("../../app/ui/static/views/assist.js");

test("maxTurnId ignores optimistic rows without ids", () => {
  assert.equal(maxTurnId([]), 0);
  assert.equal(maxTurnId([{ id: 7 }, { content: "pending", _pending: true }, { id: 12 }]), 12);
});

test("an answer captured after the watermark is durable; the same text from before is not", () => {
  const turns = [
    { id: 10, role: "assistant", content: "## Fix\nrun this" },
    { id: 11, role: "operator", content: "root@pve:~# x" },
    { id: 12, role: "assistant", content: "I couldn't verify this step myself — …" },
  ];
  const entry = { kind: "ask", content: "I couldn't verify this step myself — … " };
  assert.equal(ephemeralIsDurable(turns, entry, 11), true);   // captured this turn → retire
  assert.equal(ephemeralIsDurable(turns, entry, 12), false);  // only an OLD identical turn → keep (§17.871)
  assert.equal(ephemeralIsDurable(turns, { content: "## Fix\nrun this" }, 11), false);
  assert.equal(ephemeralIsDurable(turns, { content: "   " }, 0), true); // nothing to show
});
