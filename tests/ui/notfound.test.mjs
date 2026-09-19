// §17.1114 (ledger U-1) — an unknown route is a visible page that names the address.
import { test } from "node:test";
import assert from "node:assert/strict";

const { notFoundMessage } = await import("../../app/ui/static/views/notfound.js");

test("the message names the bad address as a hash route", () => {
  assert.equal(notFoundMessage("/output/abc"), "There is no page at #/output/abc.");
  assert.equal(notFoundMessage("output/abc"), "There is no page at #/output/abc.");
  assert.equal(notFoundMessage(""), "There is no page at #/.");
  assert.equal(notFoundMessage(undefined), "There is no page at #/.");
});
