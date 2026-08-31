// §17.814 — SPA util.js unit tests (node:test; dev-only, zero runtime deps).
// Run: make test-ui   (or: node --test tests/ui/)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mdToHtml, shortId, fmtUsd, timeAgo } from "../../app/ui/static/util.js";

// ── mdToHtml — audit C3 regression: prose with numbers must survive ──────────

test("prose with space-delimited integers is untouched (C3)", () => {
  const out = mdToHtml("Phase 1 refines your idea. It finishes in 3 minutes.");
  assert.ok(out.includes("Phase 1 refines your idea. It finishes in 3 minutes."));
  assert.ok(!out.includes("undefined"));
});

test("numbers in prose coexist with a real fenced block", () => {
  const out = mdToHtml("Step 2 of 4 :\n\n```\nmkdir ~/x\n```\n\nThen wait 10 seconds.");
  assert.ok(out.includes("Step 2 of 4"));
  assert.ok(out.includes("<code>mkdir ~/x\n</code></pre>"));
  assert.ok(out.includes('class="md-copy"')); // §17.847 copy button on every block
  assert.ok(out.includes("Then wait 10 seconds."));
  assert.ok(!out.includes("undefined"));
});

test("fence info string is a label, never code (§17.847)", () => {
  // Operator bug: ```bash\ncmd``` rendered "bash" as line 1 of the block, so
  // copy-paste grabbed it ("achieves nothing").
  const out = mdToHtml("```bash\necho hi\n```");
  assert.ok(out.includes("<code>echo hi\n</code>"));
  assert.ok(!out.includes("<code>bash"));
  assert.ok(out.includes('<span class="md-lang">bash</span>'));
});

test("multiple fences restore in order", () => {
  const out = mdToHtml("```\none\n```\nmiddle 7 text\n```\ntwo\n```");
  const iOne = out.indexOf("one");
  const iTwo = out.indexOf("two");
  assert.ok(iOne >= 0 && iTwo > iOne);
  assert.ok(out.includes("middle 7 text"));
  assert.ok(!out.includes("undefined"));
});

test("fence content is HTML-escaped (injection-safe)", () => {
  const out = mdToHtml("```\n<script>alert(1)</script>\n```");
  assert.ok(out.includes("&lt;script&gt;"));
  assert.ok(!out.includes("<script>"));
});

test("prose HTML is escaped", () => {
  const out = mdToHtml('Hello <img src=x onerror=alert(1)> & "friends"');
  assert.ok(out.includes("&lt;img"));
  assert.ok(out.includes("&amp;"));
  assert.ok(!out.includes("<img"));
});

test("ordered lists render as <ol> (§17.854 G8)", () => {
  const out = mdToHtml("Steps:\n\n1. first\n2. second\n3. third");
  assert.ok(out.includes("<ol>"));
  assert.ok(out.includes("<li>first</li>"));
  assert.ok(out.includes("<li>third</li>"));
  assert.ok(!out.includes("1. first")); // the numeral marker is stripped
});

test("ordered list is not wrapped in a paragraph (§17.854 G8)", () => {
  const out = mdToHtml("before\n\n1. a\n2. b\n\nafter");
  assert.ok(!/<p>\s*<ol/.test(out));
  assert.ok(out.includes("<ol>"));
});

test("unordered lists still render as <ul>", () => {
  const out = mdToHtml("- one\n- two");
  assert.ok(out.includes("<ul>") && out.includes("<li>one</li>"));
});

test("link URL cannot break out of href and inject attributes (§17.854)", () => {
  const out = mdToHtml('[click me](https://x.example/a" onmouseover="alert(1))');
  // the quote must arrive entity-escaped, so the raw breakout sequence and the
  // resulting live attribute can never appear
  assert.ok(out.includes("&quot;"));
  assert.ok(!out.includes('" onmouseover="'));
});

test("link URL cannot override rel=noopener (§17.854)", () => {
  const out = mdToHtml('[x](https://evil.example/" rel="opener)');
  assert.ok(!out.includes('rel="opener"'));
  assert.ok(out.includes('rel="noopener"'));
});

test("a literal NUL in source cannot forge a stash reference", () => {
  const out = mdToHtml("evil \x00MD0\x00 text with no fences");
  assert.ok(!out.includes("undefined"));
  assert.ok(!out.includes("md-pre"));
});

test("headings, bold, em, inline code, links, lists", () => {
  const out = mdToHtml(
    "## Title\n\nSome **bold** and *em* and `code` and [a link](https://x.example/y).\n\n- item one\n- item two");
  assert.ok(out.includes("<h2>Title</h2>"));
  assert.ok(out.includes("<strong>bold</strong>"));
  assert.ok(out.includes("<em>em</em>"));
  assert.ok(out.includes("<code>code</code>"));
  assert.ok(out.includes('href="https://x.example/y"'));
  assert.ok(out.includes("<li>item one</li><li>item two</li>"));
});

// ── §17.890 — bare URLs autolink; code spans/fences/md-links stay untouched ──

test("a bare URL becomes a real link (§17.890)", () => {
  const out = mdToHtml("See https://docs.docker.com/engine/install/ for details.");
  assert.ok(out.includes(
    '<a href="https://docs.docker.com/engine/install/" rel="noopener" target="_blank">'));
});

test("bare-URL trailing sentence punctuation stays outside the link", () => {
  const out = mdToHtml("Go to https://example.com/a?b=1&c=2, then stop.");
  assert.ok(out.includes('href="https://example.com/a?b=1&amp;c=2"'));
  assert.ok(!out.includes('href="https://example.com/a?b=1&amp;c=2,'));
});

test("a URL inside inline code is NOT linkified", () => {
  const out = mdToHtml("Run `curl https://example.com/i.sh` first.");
  assert.ok(out.includes("<code>curl https://example.com/i.sh</code>"));
  assert.ok(!out.includes("<code>curl <a"));
});

test("a URL inside a fence is NOT linkified", () => {
  const out = mdToHtml("```bash\ncurl https://example.com/i.sh | sh\n```");
  assert.ok(!out.includes("<a href"));
});

test("a markdown link is not double-linkified", () => {
  const out = mdToHtml("Read [the docs](https://example.com/x) now.");
  assert.equal((out.match(/<a /g) || []).length, 1);
  assert.ok(out.includes(">the docs</a>"));
});

test("a quoted bare URL stops at the closing quote entity", () => {
  const out = mdToHtml('It said "https://example.com/path" in the log.');
  assert.ok(out.includes('href="https://example.com/path"'));
  assert.ok(!out.includes("quot;</a>") && !out.includes('href="https://example.com/path&quot;'));
});

test("a standalone inline stash still wraps in a paragraph (§17.890)", () => {
  const out = mdToHtml("`just code`");
  assert.ok(out.includes("<p><code>just code</code></p>"));
});

test("paragraphs split on blank lines; single newlines become <br>", () => {
  const out = mdToHtml("first line\nsecond line\n\nnew para");
  assert.ok(out.includes("first line<br>second line"));
  assert.ok(out.includes("<p>new para</p>"));
});

test("a fence is never wrapped in <p>", () => {
  const out = mdToHtml("before\n\n```\ncode\n```\n\nafter");
  assert.ok(!/<p>\s*<pre/.test(out));
});

test("empty/null input returns empty string", () => {
  assert.equal(mdToHtml(""), "");
  assert.equal(mdToHtml(null), "");
});

// ── small pure helpers ───────────────────────────────────────────────────────

test("shortId truncates to 8 chars", () => {
  assert.equal(shortId("abcdefgh-1234"), "abcdefgh");
  assert.equal(shortId(""), "—");
});

test("fmtUsd shows sub-cent precision", () => {
  assert.equal(fmtUsd(0.0042), "$0.0042");
  assert.equal(fmtUsd(1.5), "$1.50");
  assert.equal(fmtUsd(null), "—");
});

test("timeAgo handles bad input", () => {
  assert.equal(timeAgo(null), "—");
  assert.equal(timeAgo("not-a-date"), "—");
});

// ── §17.820 (plan 5.9) — scenarios ported from the retired /web test suite ──

test("javascript: links are never linkified (scheme allowlist is https?: only)", () => {
  const out = mdToHtml("[click me](javascript:alert(1))");
  assert.ok(!out.includes("<a "), "javascript: URL must not become an anchor");
  assert.ok(!out.includes("javascript:alert(1)\" "), "raw scheme must not land in an attribute");
});

test("data: and file: links are never linkified either", () => {
  for (const bad of ["data:text/html,x", "file:///etc/passwd", "vbscript:x"]) {
    const out = mdToHtml(`[x](${bad})`);
    assert.ok(!out.includes("<a "), `${bad} must not become an anchor`);
  }
});

test("moveItem swaps up and down and reports success", async () => {
  const { moveItem } = await import("../../app/ui/static/util.js");
  const order = ["T1", "T2", "T3"];
  // "move T2 up" — the retired /web scenario asserted the server received
  // ["T2","T1","T3"] for dir=up on T2.
  assert.equal(moveItem(order, 1, -1), true);
  assert.deepEqual(order, ["T2", "T1", "T3"]);
  assert.equal(moveItem(order, 2, 1), false, "moving the last item down is a no-op");
  assert.deepEqual(order, ["T2", "T1", "T3"]);
  assert.equal(moveItem(order, 0, -1), false, "moving the first item up is a no-op");
});
