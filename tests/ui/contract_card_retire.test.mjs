// §17.1180 (audit U8 + U2) — two rules that existed twice, and a card rendered
// into a node the default layout never mounts.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = new URL("../../app/ui/static/views/assist.js", import.meta.url);
const raw = readFileSync(SRC, "utf8");
const src = raw.replace(/\/\*[\s\S]*?\*\//g, "")
  .split("\n").filter((ln) => !/^\s*\/\//.test(ln)).join("\n");

test("the retirement rule is defined once and used by both callers", () => {
  const defs = [...src.matchAll(/function contractRetired\(/g)];
  assert.equal(defs.length, 1, "the rule must have exactly one definition");
  const uses = [...src.matchAll(/contractRetired\(/g)];
  assert.ok(uses.length >= 3,
    `contractRetired is used ${uses.length - 1} times; both the card and load() must consult it`);
});

test("the old inline copy of the rule is gone from load()", () => {
  assert.ok(!/_sc\.committed/.test(src),
    "load() still re-implements the committed/done test instead of calling the rule");
});

test("the card is not built during mount, when session is null by construction", () => {
  // The only automatic call site ran inside the synchronous mount, before
  // load() had assigned `session` — so the guard inside contractCard could
  // never fire and the card painted for a frame before load() tore it out.
  assert.ok(!/contractCard\(null, session\)/.test(src),
    "the mount still builds the card from a null session");
  assert.ok(/contractSlot/.test(src), "the card needs a slot rendered once the session is known");
});

test("the completion card has a host that exists in BOTH layouts", () => {
  // U2: it was appended to `stepHero`, which the follow branch — the DEFAULT,
  // since job_hub routes both `run` and `follow` there — never mounts. So
  // "🎉 Job complete" and its two navigation buttons went into a detached node.
  assert.ok(/completeSlot/.test(src));
  assert.ok(!/stepHero\.append\(card\)/.test(src),
    "the completion card is still appended to the unmounted step hero");
  const follow = src.slice(src.indexOf("const main = follow"), src.indexOf("const briefSlot"));
  const branches = follow.split("\n").filter((l) => l.includes("completeSlot"));
  assert.equal(branches.length, 2, "both the follow and classic layouts must mount the slot");
});
