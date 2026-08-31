// Shared DOM + formatting helpers. Zero dependencies.

/** Create an element. `attrs` may include: class, text, html, on{Event}, dataset, and any attribute. children may be nodes/strings. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

/** Replace all children of `parent` with `nodes`. */
export function mount(parent, ...nodes) {
  parent.replaceChildren(...nodes.flat().filter((n) => n != null));
  return parent;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

/** A short 8-char id prefix for display. */
export function shortId(id) {
  return id ? String(id).slice(0, 8) : "—";
}

/** Relative time, e.g. "3m ago". Accepts ISO string or Date. */
export function timeAgo(ts) {
  if (!ts) return "—";
  const then = new Date(ts).getTime();
  if (Number.isNaN(then)) return "—";
  const s = Math.round((Date.now() - then) / 1000);
  if (s < 0) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export function fmtDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export function fmtNum(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString();
}

export function fmtUsd(n) {
  if (n == null) return "—";
  const v = Number(n);
  return v < 0.01 && v > 0 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

/** Debounce a function. */
export function debounce(fn, ms = 250) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

/** Minimal, safe markdown → HTML for read-only display (headings, bold, code,
 *  lists, links, paragraphs). Escapes HTML first so it's injection-safe.
 *
 *  §17.814 (audit C3) — fenced blocks are stashed behind a NUL-delimited
 *  sentinel (\x00MD<i>\x00), NOT a bare " <i> ": the old restore pass ran
 *  / (\d+) / over ALL text, so any space-delimited integer in ordinary prose
 *  was treated as a stash index ("Phase 1 refines... in 3 minutes" became
 *  "Phaseundefinedrefines..."). NUL can't be typed in real content, survives
 *  the HTML escape untouched, and the restore matches ONLY exact sentinels. */
export function mdToHtml(src) {
  if (!src) return "";
  // §17.854 — quotes MUST be escaped: the link rule below interpolates the
  // captured URL into href="...", so an unescaped `"` in LLM-emitted text lets
  // a crafted link inject attributes (e.g. override rel="noopener").
  const esc = (s) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  // fenced code blocks — stashed on their own paragraph so the wrapper below
  // never buries a <pre> inside a <p>.
  const blocks = [];
  // §17.847 — the fence info string (```bash) is metadata, NOT code: it used
  // to land inside the block, so copy-pasting a walkthrough command grabbed
  // "bash" as line 1 ("achieves nothing" — operator). Strip it, keep it as a
  // label, and give every block a copy button (delegated handler in app.js —
  // strict CSP forbids inline handlers).
  let text = String(src).replace(/```([^\n]*)\n?([\s\S]*?)```/g, (_, info, code) => {
    const lang = esc(info.trim().split(/\s+/)[0] || "");
    blocks.push(
      `<pre class="md-pre">` +
      `<span class="md-pre-bar">${lang ? `<span class="md-lang">${lang}</span>` : ""}` +
      `<button class="md-copy" type="button" title="Copy to clipboard">⧉ copy</button></span>` +
      `<code>${esc(code.replace(/^\n/, ""))}</code></pre>`
    );
    return `\n\n\x00MD${blocks.length - 1}\x00\n\n`;
  });
  text = esc(text);
  text = text
    .replace(/^###### (.*)$/gm, "<h6>$1</h6>")
    .replace(/^##### (.*)$/gm, "<h5>$1</h5>")
    .replace(/^#### (.*)$/gm, "<h4>$1</h4>")
    .replace(/^### (.*)$/gm, "<h3>$1</h3>")
    .replace(/^## (.*)$/gm, "<h2>$1</h2>")
    .replace(/^# (.*)$/gm, "<h1>$1</h1>")
    // inline code — stashed like fences so a URL or emphasis inside `...` is
    // never rewritten by the passes below (§17.890).
    .replace(/`([^`\x00]+)`/g, (_, code) => {
      blocks.push(`<code>${code}</code>`);
      return `\x00MD${blocks.length - 1}\x00`;
    })
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    // markdown links — stashed so the bare-URL pass below can't re-linkify
    // the href it just produced.
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, (_, label, url) => {
      blocks.push(`<a href="${url}" rel="noopener" target="_blank">${label}</a>`);
      return `\x00MD${blocks.length - 1}\x00`;
    });
  // §17.890 — bare URLs become REAL links (research answers and fixes emit
  // plain https://… constantly; they rendered as dead text). The text is
  // already HTML-escaped, so cut the match at the first escaped delimiter
  // entity (a literal ", ', < or > can't appear unencoded in a URL), then
  // shed trailing punctuation that belongs to the sentence, not the URL.
  text = text.replace(/https?:\/\/[^\s\x00]+/gi, (m) => {
    const cut = m.search(/&(?:quot|#39|lt|gt);/);
    let url = cut === -1 ? m : m.slice(0, cut);
    const tail = m.slice(url.length);
    const trimmed = url.replace(/[.,;:!?)\]]+$/, "");
    const rest = url.slice(trimmed.length) + tail;
    if (!trimmed) return m;
    return `<a href="${trimmed}" rel="noopener" target="_blank">${trimmed}</a>${rest}`;
  });
  // ordered lists (§17.854 audit G8 — assist guidance arrives as `1. … 2. …`
  // numbered steps; without this they flattened to <p> with <br>, losing the
  // step structure in the surface most dependent on it). Runs BEFORE unordered
  // so a `1.` line isn't consumed by the bullet rule.
  text = text.replace(/(?:^|\n)((?:\d+\. .*(?:\n|$))+)/g, (m, list) => {
    const items = list.trim().split(/\n/).map((li) => `<li>${li.replace(/^\d+\. /, "")}</li>`).join("");
    return `\n<ol>${items}</ol>`;
  });
  // unordered lists
  text = text.replace(/(?:^|\n)((?:[-*] .*(?:\n|$))+)/g, (m, list) => {
    const items = list.trim().split(/\n/).map((li) => `<li>${li.replace(/^[-*] /, "")}</li>`).join("");
    return `\n<ul>${items}</ul>`;
  });
  // paragraphs / line breaks (a stashed fence stands alone — never wrapped).
  // §17.890 — inline stashes (code spans, links) share the sentinel now, so
  // resolve a standalone sentinel and only skip wrapping for true blocks
  // (<pre>); a paragraph that merely STARTS with an inline stash still wraps.
  text = text
    .split(/\n{2,}/)
    .map((p) => {
      const solo = p.match(/^\s*\x00MD(\d+)\x00\s*$/);
      const isBlock = solo
        ? (blocks[Number(solo[1])] || "").startsWith("<pre")
        : /^\s*<(h\d|ul|ol|pre|blockquote)/.test(p);
      return isBlock ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`;
    })
    .join("\n");
  text = text.replace(/\x00MD(\d+)\x00/g, (_, i) => blocks[Number(i)] ?? "");
  return text;
}

/** §17.890 — right-click copy was effectively impossible in the chat views:
 * every streamed frame force-scrolled the transcript to the bottom, and full
 * re-renders destroyed the active selection out from under the native context
 * menu (its Copy then acts on a selection that no longer exists; a fast
 * Ctrl+C raced the churn and usually won — hence "Ctrl+C works, right-click
 * doesn't"). Two helpers fix the class:
 * - stickyScroll(elm): returns a scroll-to-bottom fn that only fires while
 *   the user is already pinned near the bottom — reading back or selecting
 *   above never gets yanked.
 * - selectionWithin(elm): true while the user holds a live selection inside
 *   elm — callers defer destructive re-renders until it clears. */
export function stickyScroll(elm) {
  let pinned = true;
  elm.addEventListener("scroll", () => {
    pinned = elm.scrollHeight - elm.scrollTop - elm.clientHeight < 48;
  });
  return () => { if (pinned) elm.scrollTop = elm.scrollHeight; };
}

export function selectionWithin(elm) {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
  const n = sel.getRangeAt(0).commonAncestorContainer;
  return elm.contains(n.nodeType === 1 ? n : n.parentNode);
}

/** Copy text to clipboard; returns a promise. */
export async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** §17.820 — swap arr[i] with arr[i+delta] in place (reorder verbs).
 * Returns true if the swap happened, false when it would fall off either
 * end. Pure array semantics extracted from the plan view so the reorder
 * permutation the server receives is unit-testable. */
export function moveItem(arr, i, delta) {
  const j = i + delta;
  if (j < 0 || j >= arr.length || i < 0 || i >= arr.length) return false;
  [arr[i], arr[j]] = [arr[j], arr[i]];
  return true;
}
