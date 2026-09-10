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
