// §17.1180 (audit U3) — the approval gate must not silently drop one of the
// engine's explicit `clarifications_needed`.
//
// `dedupeQuestions` used the OVERLAP COEFFICIENT (inter / min(|a|,|b|)) at 0.5.
// That merges questions differing in the only word that matters, and the min()
// denominator makes a short question a subset-match of a longer one — so the
// MORE SPECIFIC question is the one that loses.
import { test } from "node:test";
import assert from "node:assert/strict";

const { dedupeQuestions, QUESTION_DUPLICATE_JACCARD } =
  await import("../../app/ui/static/views/approvals.js");

test("two questions that differ only in the noun both survive", () => {
  // the measured case: overlap coefficient scored these 3/4 = 0.75 and dropped
  // the second, so the operator was never asked about the network backend.
  const qs = [
    "Which storage backend should it use?",
    "Which network backend should it use?",
  ];
  assert.deepEqual(dedupeQuestions(qs), qs);
});

test("a short question is not swallowed by a longer one that contains it", () => {
  const qs = [
    "Which backend?",
    "Which backend should the deployment use for persistent volumes?",
  ];
  assert.equal(dedupeQuestions(qs).length, 2, "min() made the short one a subset match");
});

test("genuine restatements still collapse to one", () => {
  const kept = dedupeQuestions([
    "Which storage backend should it use?",
    "Which storage backend should we use?",
  ]);
  assert.equal(kept.length, 1);
});

test("an exact repeat collapses, and the longer wording wins", () => {
  // the real duplicate shape: the same question arriving from both
  // `clarifications_needed` and `ambiguities`, punctuated differently.
  const kept = dedupeQuestions([
    "What is the target host?",
    "What is the target host",
  ]);
  assert.equal(kept.length, 1);
  assert.equal(kept[0], "What is the target host?", "the longer wording wins");
});

test("an added qualifier is KEPT, and that is the deliberate asymmetry", () => {
  // "What is the target host?" vs "What is the target host, precisely?" scores
  // 3/4 = 0.75 — under the threshold, so both are shown. At the approval gate
  // a near-duplicate the operator answers once is the cheap failure; dropping
  // a question the engine explicitly asked is the expensive one.
  const kept = dedupeQuestions([
    "What is the target host?",
    "What is the target host, precisely?",
  ]);
  assert.equal(kept.length, 2);
});

test("distinct questions are never merged", () => {
  const qs = [
    "How many VLANs do you need?",
    "Which storage backend should it use?",
    "What is the target host?",
  ];
  assert.deepEqual(dedupeQuestions(qs), qs);
});

test("the threshold is high on purpose — this is the approval gate", () => {
  // Dropping a question the engine explicitly asked is worse than showing a
  // near-duplicate the operator answers once. If someone lowers this, the
  // asymmetry above stops holding.
  assert.ok(QUESTION_DUPLICATE_JACCARD >= 0.8,
    `threshold ${QUESTION_DUPLICATE_JACCARD} is low enough to merge distinct questions`);
});

test("empty and single inputs are unchanged", () => {
  assert.deepEqual(dedupeQuestions([]), []);
  assert.deepEqual(dedupeQuestions(["Only one?"]), ["Only one?"]);
});
