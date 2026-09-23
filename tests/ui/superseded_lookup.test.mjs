// §17.1166 — a look-up the engine ran itself is not work for the operator.
// Live (ADD65, 2026-09-22): the walkthrough said "Run this now: `nvidia-smi`";
// 0.2 s later the runner ran it and printed the answer; at 22:53:00 the
// operator ran and pasted it anyway, because the ask was still standing.
import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.localStorage = { _m: new Map(), getItem(k) { return this._m.get(k) ?? null; }, setItem(k, v) { this._m.set(k, v); }, removeItem(k) { this._m.delete(k); } };

// a DOM small enough to be honest about what the annotator touches
class FakeEl {
  constructor(cls = "", text = "") {
    this.className = cls; this.text = text; this.children = []; this.dataset = {}; this.parentNode = null;
    this.classList = { add: (c) => { this.className = (this.className + " " + c).trim(); },
                       remove() {}, toggle() {},
                       contains: (c) => this.className.split(/\s+/).includes(c) };
  }
  get textContent() { return this.text || this.children.map((c) => c.textContent).join("\n"); }
  set textContent(v) { this.text = v; this.children = []; }
  append(...kids) { kids.forEach((k) => { k.parentNode = this; this.children.push(k); }); return this; }
  appendChild(k) { return this.append(k); }
  setAttribute() {} addEventListener() {} removeEventListener() {} remove() {}
  insertBefore(node, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? 0 : i, 0, node); node.parentNode = this; }
  _all(sel, out = []) {
    this.children.forEach((c) => { if (c.matches(sel)) out.push(c); c._all(sel, out); });
    return out;
  }
  matches(sel) {
    if (sel === "pre") return this.tag === "pre";
    return sel.startsWith(".") && this.classList.contains(sel.slice(1));
  }
  querySelectorAll(sel) { return this._all(sel); }
  querySelector(sel) { return this._all(sel)[0] || null; }
}
const noopEl = () => new FakeEl();
globalThis.document = { createElement: noopEl, createTextNode: noopEl, createDocumentFragment: noopEl,
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
  body: noopEl(), documentElement: noopEl() };
globalThis.window = { location: { hash: "" }, addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) };
globalThis.queueMicrotask = globalThis.queueMicrotask || ((f) => f());

const { lookupCommandsFrom, annotateSupersededLookups, RUNNER_LOOKUP_NOTE_RE } =
  await import("../../app/ui/static/views/assist.js");

function msg(cls, bodyText, pres = []) {
  const m = new FakeEl(`msg ${cls}`);
  const body = new FakeEl("msg-body md", bodyText);
  pres.forEach((p) => { const pre = new FakeEl("", p); pre.tag = "pre"; body.append(pre); });
  if (pres.length) body.text = "";
  m.append(body);
  return m;
}

const NOTE = `🔁 Your local runner is connected, so I ran that look-up myself (1 read-only command through pve-runner):

$ qm status 106
status: stopped`;

test("the commands the engine answered are read out of the note", () => {
  assert.deepEqual([...lookupCommandsFrom(NOTE)], ["qm status 106"]);
  assert.equal(lookupCommandsFrom("no dollar lines here").size, 0);
  // the durable operator turn carries the same shape
  assert.ok(RUNNER_LOOKUP_NOTE_RE.test("[local-runner] ran the walkthrough's read-only look-up through your local runner (pve-runner):"));
  assert.ok(RUNNER_LOOKUP_NOTE_RE.test(NOTE));
  assert.ok(!RUNNER_LOOKUP_NOTE_RE.test("🩺 State check result — 32 confirmed"));
});

test("the ask the engine already answered is retired, and only that one", () => {
  const root = new FakeEl("transcript");
  const guide = msg("as", "", ["qm status 106"]);
  root.append(guide, msg("as", NOTE));
  annotateSupersededLookups(root);
  const pre = guide.querySelectorAll("pre")[0];
  assert.equal(pre.dataset.superseded, "1");
  assert.ok(pre.classList.contains("superseded"));
  const badge = guide.querySelector(".superseded-badge");
  assert.ok(badge && /already ran this/.test(badge.textContent));
});

test("a block the engine did NOT fully answer is left alone", () => {
  const root = new FakeEl("transcript");
  // two commands, the runner answered one — the operator still has work to do
  const guide = msg("as", "", ["qm status 106\nqm agent 106 ping"]);
  root.append(guide, msg("as", NOTE));
  annotateSupersededLookups(root);
  assert.equal(guide.querySelectorAll("pre")[0].dataset.superseded, undefined);
  assert.equal(guide.querySelector(".superseded-badge"), null);
});

test("it is idempotent and never marks twice across re-renders", () => {
  const root = new FakeEl("transcript");
  const guide = msg("as", "", ["qm status 106"]);
  root.append(guide, msg("as", NOTE));
  annotateSupersededLookups(root);
  annotateSupersededLookups(root);
  assert.equal(guide.querySelectorAll(".superseded-badge").length, 1);
});

test("a note with no ask above it does nothing", () => {
  const root = new FakeEl("transcript");
  root.append(msg("as", NOTE));
  annotateSupersededLookups(root);       // must not throw
  assert.equal(root.querySelector(".superseded-badge"), null);
});
