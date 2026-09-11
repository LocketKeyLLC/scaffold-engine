// §17.1011 — walkthrough progressive disclosure.
//
// The fixture is the REAL guidance the live homelab job generated for node
// T35 ("Configure reverse proxy") — 4,817 chars over nine top-level sections,
// internally split into Phase A/B/C. Testing against invented markdown would
// prove the splitter handles markdown I wrote to suit it; the whole point is
// that it handles what the engine actually emits.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const T35 = readFileSync(join(here, "fixtures", "guidance_t35.md"), "utf8");

const { splitGuideSections, sectionCount, GUIDE_OPEN_SECTION } =
  await import("../../app/ui/static/views/assist.js");

test("splits the real T35 walkthrough into its top-level sections", () => {
  const { sections } = splitGuideSections(T35);
  const titles = sections.map((s) => s.title);
  // The nine sections the operator was shown at once.
  assert.ok(titles.some((t) => /Do this next/i.test(t)), "missing 👉 lead");
  for (const want of ["Prerequisites", "Inputs needed", "Run this", "Verify",
                      "Rollback", "Risk"]) {
    assert.ok(titles.some((t) => t.includes(want)), `missing section: ${want}`);
  }
  assert.ok(titles.some((t) => /Done when/i.test(t)), "missing ✅ close");
  assert.ok(sections.length >= 8, `expected the real 9-section shape, got ${sections.length}`);
});

test("folds most of T35 away but keeps the action and the finish line open", () => {
  const { sections } = splitGuideSections(T35);
  const open = sections.filter((s) => GUIDE_OPEN_SECTION.test(s.title));
  const folded = sections.filter((s) => !GUIDE_OPEN_SECTION.test(s.title));

  // Exactly the two the operator needs to act: what to do, and when it's done.
  assert.equal(open.length, 2, `expected 2 open sections, got ${open.map((s) => s.title)}`);
  assert.ok(open.some((s) => /Do this next/i.test(s.title)));
  assert.ok(open.some((s) => /Done when/i.test(s.title)));

  // And the bulk is behind a row rather than on screen.
  assert.ok(folded.length >= 6, `expected the runbook body folded, got ${folded.length}`);
  const openChars = open.reduce((n, s) => n + s.body.join("\n").length, 0);
  assert.ok(openChars < T35.length / 3,
    `lead should be a fraction of the whole: ${openChars} of ${T35.length}`);
});

test("### sub-headings stay inside their parent section", () => {
  // T35's "Run this" contains "### Phase A/B/C" — those must NOT each become
  // their own disclosure row, or folding just re-creates the wall of rows.
  const { sections } = splitGuideSections(T35);
  const titles = sections.map((s) => s.title);
  assert.ok(!titles.some((t) => /^Phase [ABC]/.test(t)),
    `sub-headings leaked to top level: ${titles.filter((t) => /Phase/.test(t))}`);
  const runThis = sections.find((s) => /Run this/i.test(s.title));
  assert.ok(runThis && /Phase A/.test(runThis.body.join("\n")),
    "Phase A should live inside the Run this section");
});

test("a ## inside a fenced code block is not a section heading", () => {
  const md = [
    "## 👉 Do this next", "run it", "",
    "## Run this", "```bash", "# not a heading", "## also not a heading",
    "echo hi", "```", "done",
  ].join("\n");
  const { sections } = splitGuideSections(md);
  assert.equal(sections.length, 2, `fence leaked headings: ${sections.map((s) => s.title)}`);
});

test("guidance with no headings is left whole (nothing to fold)", () => {
  const { lead, sections } = splitGuideSections("Just do the thing.\n\nThen tell me.");
  assert.equal(sections.length, 0);
  assert.match(lead, /Just do the thing/);
});

test("sectionCount counts list items, and stays silent for prose", () => {
  assert.equal(sectionCount("- a\n- b\n- c"), " (3)");
  assert.equal(sectionCount("1. a\n2. b"), " (2)");
  assert.equal(sectionCount("just a sentence"), "");
  assert.equal(sectionCount("- only one"), "");
});

// ── §17.1013 — a FIX turn folds by its own shape ─────────────────────────
// The fixture is the real fix turn the operator was looking at when they
// reported "without ANY walk through, though it is hidden under Fix". A guide
// puts the whole immediate action in `## 👉 Do this next`, so folding
// `## Run this` under it loses nothing; a fix puts a one-line summary there and
// the actual steps in `## Fix`, so the walkthrough rule hid them.
const FIXTURE_FIX = readFileSync(join(here, "fixtures", "fix_turn_t35.md"), "utf8");
const { openSectionFor, FIX_OPEN_SECTION } =
  await import("../../app/ui/static/views/assist.js");

test("the real fix turn keeps its action section open", () => {
  const { sections } = splitGuideSections(FIXTURE_FIX);
  const re = openSectionFor("fix");
  const open = sections.filter((s) => re.test(s.title)).map((s) => s.title);
  const folded = sections.filter((s) => !re.test(s.title)).map((s) => s.title);

  assert.ok(open.some((t) => /^Fix$/i.test(t)),
    `the 'Fix' section (the walkthrough) must stay open, got open=${open}`);
  assert.ok(open.some((t) => /Do this next/i.test(t)));
  // Context stays folded — the point is still to quiet the bubble.
  for (const ctx of ["Diagnosis", "Then", "If that fails"]) {
    assert.ok(folded.includes(ctx), `${ctx} should fold, got folded=${folded}`);
  }
});

test("folding a fix reveals materially more than the guide rule did", () => {
  const { lead, sections } = splitGuideSections(FIXTURE_FIX);
  const visible = (re) => lead.trim().length + sections
    .filter((s) => re.test(s.title))
    .reduce((a, s) => a + s.body.join("\n").trim().length, 0);
  // The regression: under the guide rule only the 👉 one-liner survived.
  assert.ok(visible(openSectionFor("fix")) > visible(openSectionFor("guide")) * 1.8,
    "the fix rule must surface the steps the guide rule hid");
});

test("guide turns are unaffected by the fix rule", () => {
  const { sections } = splitGuideSections(T35);
  const re = openSectionFor("guide");
  const open = sections.filter((s) => re.test(s.title)).map((s) => s.title);
  assert.equal(open.length, 2);          // 👉 + ✅ Done when, as before
  assert.ok(!open.some((t) => /Run this/i.test(t)), "Run this must still fold");
});

test("openSectionFor defaults to the walkthrough rule", () => {
  for (const k of ["guide", "message", "ask", undefined, null, ""]) {
    assert.equal(openSectionFor(k), GUIDE_OPEN_SECTION, `kind ${k}`);
  }
  assert.equal(openSectionFor("fix"), FIX_OPEN_SECTION);
});
