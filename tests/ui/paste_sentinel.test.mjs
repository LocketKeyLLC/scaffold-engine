// §17.1159 — the block hash the paste sentinel carries must match the Python
// twin (app/modules/assist_paste.py block_hash) byte for byte.
import test from "node:test";
import assert from "node:assert/strict";
import { blockHash, sentinelFor } from "../../app/ui/static/util.js";

test("blockHash matches the Python fixture and ignores whitespace, comments and a sentinel line", () => {
  assert.equal(blockHash("qm status 110\nqm config 110"), "e958581d");          // tests/test_assist_paste.py pins the same value
  assert.equal(blockHash("  qm status 110 \n\n# note\nqm config 110\n"), "e958581d");
  assert.equal(blockHash('qm status 110\nqm config 110\necho "== S:ADD49/e958581d =="'), "e958581d");
  assert.notEqual(blockHash("qm status 110"), "e958581d");
});

test("sentinelFor is the echo line the copy button appends", () => {
  assert.equal(sentinelFor("ADD49", "qm status 110\nqm config 110"), 'echo "== S:ADD49/e958581d =="');
});
