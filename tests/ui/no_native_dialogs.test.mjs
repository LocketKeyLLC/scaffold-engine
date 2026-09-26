// §17.1180 (audit U6) — the SPA must not use the browser's own dialogs.
//
// The audit named two sites, because those were the two that already imported
// this project's accessible `openDialog` and reached past it anyway. Scanning
// for the class found SIX more in views that had never imported it:
// models, schedules, plan (×2), research and theater.
//
// Native dialogs take focus out of the SPA, cannot be themed or styled, block
// the event loop, and are suppressed outright in some embedded contexts — in
// which case the operator's click silently does nothing. `window.prompt` is
// worse still: assist used it to ask the operator to RECALL AND TYPE a step key
// the engine was already holding a pre-image for.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../app/ui/static/", import.meta.url).pathname;

function jsFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...jsFiles(p));
    else if (name.endsWith(".js")) out.push(p);
  }
  return out;
}

/** Source with COMMENTS removed — scan code, not prose.
 *
 * Deliberately does NOT strip string literals: a naive literal regex cannot
 * parse nested template literals, and on `assist.js` it swallowed whole
 * regions including the very call this file asserts is present. Comments were
 * the real source of false positives (these files explain the defect by
 * name), and no view passes a bare "confirm(" inside a string. */
function code(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n").filter((ln) => !/^\s*\/\//.test(ln)).join("\n");
}

const NATIVE = /(?:^|[^.\w$])(?:window\.)?(confirm|prompt|alert)\s*\(/;

// §17.1180 — the ONE documented exception. `router.js`'s nav guard must decide
// synchronously whether to allow a hashchange; an async dialog cannot block
// one, so honouring a "you have unsaved work" guard with `askConfirm` would
// mean letting the navigation happen and then undoing it — a change to
// navigation semantics, not a dialog swap. `navAllowed` already takes its
// confirm function as a parameter, so the decision is testable without the
// browser. Revisit only with a real unsaved-work flow to test against.
const SYNC_NAV_GUARD = "router.js";

test("no view calls the browser's confirm, prompt or alert", () => {
  const offenders = [];
  for (const f of jsFiles(ROOT)) {
    if (f.slice(ROOT.length) === SYNC_NAV_GUARD) continue;
    const src = code(readFileSync(f, "utf8"));
    for (const [i, ln] of src.split("\n").entries()) {
      const m = ln.match(NATIVE);
      if (m) offenders.push(`${f.slice(ROOT.length)}:${i + 1}  ${m[1]}()  ${ln.trim().slice(0, 70)}`);
    }
  }
  assert.deepEqual(offenders, [],
    "use askConfirm / askChoice from components.js:\n  " + offenders.join("\n  "));
});

test("the accessible replacements exist and are exported", async () => {
  const c = await import("../../app/ui/static/components.js");
  assert.equal(typeof c.askConfirm, "function");
  assert.equal(typeof c.askChoice, "function");
});

test("askChoice is what the restore verb uses, so no key is typed from memory", () => {
  const src = code(readFileSync(join(ROOT, "views/assist.js"), "utf8"));
  assert.ok(/askChoice\(/.test(src), "the restore verb must offer a picker");
  assert.ok(/restorable_steps/.test(src),
    "the picker must be populated from what the engine already knows");
});


test("the nav-guard exception is still just the nav guard", () => {
  // If router.js grows a second native dialog, the blanket skip above would
  // hide it. Pin the exemption to the one call that needs to be synchronous.
  const src = code(readFileSync(join(ROOT, SYNC_NAV_GUARD), "utf8"));
  const hits = [...src.matchAll(/(?:window\.)?(confirm|prompt|alert)\s*\(/g)];
  assert.equal(hits.length, 1, `router.js has ${hits.length} native dialogs; only the nav guard is exempt`);
  assert.ok(/navAllowed\([^)]*window\.confirm/s.test(src) || /confirmFn/.test(src),
    "the exempt call must still be the nav guard's injected confirm");
});
