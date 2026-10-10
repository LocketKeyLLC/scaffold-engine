"""§17.1443 — a page's text for a prompt is the part about the query, not its first 2000 characters.

Assist research cut every fetched page to `text[:2000]`. ADD4's router walkthrough fetched PureVPN's
"Spectrum port forwarding" article (12,196 chars): the first 2000 were "What is port forwarding? Should you
enable UPnP?", and the My Spectrum app steps — "Navigate to Services > Router > Advanced Settings > Port
Forwarding & IP Reservations. Tap Add Port Assignment. … Device IP (assign a static IP to the target device
first)" — sat at char ~8,000. The model never saw them; it wrote "Tap Add Port Forwarding" and left out
the reservation the app requires first, which is the exact point the operator got stuck on.

`focus(text, query, budget)` keeps a page's best-matching stretches in document order: each line is scored
by the query's distinctive words it contains (an instruction line — Tap/Click/Navigate…, a numbered or
bulleted item, an `A > B` path — scores extra when it matches anything), and the best lines are taken with
the lines that follow them, because a procedure's steps come after the line that names it.
"""
from __future__ import annotations

import math
import re

_STOP = frozenset("""
a an and are as at be by can do does for from get how i if in into is it its me my no not of on or our
so that the their them then there these this to up use using was we what when where which who why will
with you your steps step guide page help support how-to tutorial
""".split())
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'.+-]*[a-z0-9]|[a-z0-9]")
_INSTRUCTION_RE = re.compile(
    r"^\s*(?:[-*•]|\d+[.)])?\s*(?:tap|click|select|choose|navigate|go to|open|enter|type|press|save|"
    r"log ?in|sign in|set|turn|enable|disable|add|assign|reserve)\b|\s>\s|→", re.I)
_LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+\S")
_GAP = "\n…\n"
_SPAN_CHARS = 800  # kept after an anchor: the procedure under the line that names it
_LEAD = 1          # and the line before it (its heading)


def _terms(query: str) -> set[str]:
    return {w.strip(".'") for w in _WORD_RE.findall((query or "").lower())
            if len(w) >= 3 and w not in _STOP}


def focus(text: str, query: str, budget: int = 2000) -> str:
    """`text` if it fits; else its stretches about `query`, in page order, within `budget` chars."""
    text = text or ""
    if len(text) <= budget:
        return text
    terms = _terms(query)
    lines = text.split("\n")
    if not terms or len(lines) < 3:
        return text[:budget]
    lows = [ln.lower() for ln in lines]
    # A word on every line ("port forwarding" in a port-forwarding article) tells lines apart less than
    # one on a few ("app", "SAX1V1K"): weight each by how rare it is on this page.
    df = {t: sum(1 for low in lows if t in low) for t in terms}
    weight = {t: math.log((len(lines) + 1) / (df[t] + 0.5)) for t in terms if df[t]}
    scores = []
    for ln, low in zip(lines, lows, strict=True):
        hit = sum(w for t, w in weight.items() if t in low)
        if hit and (_INSTRUCTION_RE.search(ln) or _LIST_RE.match(ln)):
            hit *= 1.5
        scores.append(hit)
    # A stretch is ranked by what it holds per character, so a procedure (a heading + its short numbered
    # steps) beats a long paragraph that happens to repeat the query.
    spans = []
    for a, s in enumerate(scores):
        if s <= 0:
            continue
        lo, hi, size = max(0, a - _LEAD), a + 1, len(lines[a])
        while hi < len(lines) and size + len(lines[hi]) <= _SPAN_CHARS:
            size += len(lines[hi]) + 1
            hi += 1
        size += sum(len(lines[i]) for i in range(lo, a))
        spans.append((sum(scores[lo:hi]) / (1 + size / 400), a, lo, hi))
    if not spans:
        return text[:budget]
    spans.sort(key=lambda x: (-x[0], x[1]))
    keep: set[int] = set()
    used = 0
    for _, a, lo, hi in spans:
        span = [i for i in range(lo, hi) if i not in keep]
        if not span:
            continue
        cost = sum(len(lines[i]) + 1 for i in span) + len(_GAP)
        if used + cost > budget:
            if a not in keep and used + len(lines[a]) + 1 + len(_GAP) <= budget:
                keep.add(a)
                used += len(lines[a]) + 1 + len(_GAP)
            continue
        keep.update(span)
        used += cost
    out, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append(_GAP.strip("\n"))
        out.append(lines[i])
        prev = i
    return "\n".join(out)[:budget]
