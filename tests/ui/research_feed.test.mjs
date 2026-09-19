// §17.1121 — the research feed renders the agent's real payloads (shapes copied
// from app/modules/research_agent.py `_sse(...)` literals).
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const noopEl = () => ({ appendChild() {}, append() {}, remove() {}, setAttribute() {}, addEventListener() {}, removeEventListener() {},
  querySelector: () => null, querySelectorAll: () => [], classList: { add() {}, remove() {}, toggle() {}, replace() {} }, style: {}, dataset: {}, children: [], insertBefore() {} });
globalThis.document = { createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {}, body: noopEl(), documentElement: noopEl() };
globalThis.window = { location: { hash: "" }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };

const { feedText } = await import("../../app/ui/static/views/research.js");

test("extraction, search and ingestion use the emitted field names — no '?'", () => {
  assert.equal(feedText("extraction_complete", { iteration: 2, entries_extracted: 12 }).text, "Extracted 12 entries");
  assert.equal(feedText("search_complete", { iteration: 1, results_found: 30, total_urls: 30 }).text, "Search: 30 results (30 URLs so far)");
  const ing = feedText("ingestion_complete", { iteration: 2, entries_ingested: 214, total_ingested: 449, total_rejected: 3, new: 400, versioned: 46, rejected: 3, skipped_hash: 0 });
  assert.equal(ing.text, "Ingested 214 this iteration — 449 total (new 400, versioned 46, rejected 3)");
  // the other emitter shape (no new/versioned) still reads cleanly
  assert.equal(feedText("ingestion_complete", { iteration: 1, entries_ingested: 235, total_ingested: 235, total_rejected: 1 }).text,
    "Ingested 235 this iteration — 235 total (rejected 1)");
  for (const ev of ["extraction_complete", "search_complete", "ingestion_complete"]) {
    assert.ok(!feedText(ev, { entries_extracted: 1, results_found: 1, total_urls: 1, entries_ingested: 1, total_ingested: 1 }).text.includes("?"));
  }
});

test("fetch progress is a keyed line, updated in place", () => {
  const r = feedText("research_fetch", { iteration: 1, fetched: 12, total: 30, ok: 10, failed: 2, last_url: "https://x" });
  assert.equal(r.key, "fetch-1");
  assert.match(r.text, /Fetching pages 12\/30 — ok 10, failed 2/);
});

test("formerly raw events have sentences; heartbeat and progress are not lines", () => {
  assert.match(feedText("extractor_fallback", { from: "trafilatura", to: "readability", reason: "empty" }).text, /Extractor fallback trafilatura → readability: empty/);
  assert.match(feedText("content_truncated", { count: 3, max_chars: 8000, mode: "topic" }).text, /truncated to 8000 chars \(3 entries, topic\)/);
  assert.match(feedText("distill_bypassed", { url: "https://a", chunks: 4, source_type: "html" }).text, /Distill bypassed for https:\/\/a/);
  assert.equal(feedText("heartbeat", {}), null);
  assert.equal(feedText("progress", { done: 3 }), null);
});

test("completion summarises with the payload's own counters", () => {
  const t = feedText("research_complete", { total_ingested: 449, total_entries: 449, duration_ms: 407900, iterations: 2, total_urls_searched: 47 }).text;
  assert.equal(t, "Complete — 449 entries ingested of 449 in 407.9s (2 iterations, 47 URLs)");
});
