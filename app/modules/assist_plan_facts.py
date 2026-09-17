"""§17.1089 — a FACT that blocks a pending step is a plan trigger.

The reconciler's triggers were notes, decisions, confirmed fixes and re-pins
(§17.1043–1048). A fact — the scribe's record of what the operator's system
IS — had no door: "Recurring transactions are not available on Simple Start"
sat in the ledger while step T7 "Set up recurring invoices" stayed pending,
and on the homelab "CT 120 was created with a static IP" sat beside a step
that needed the router to see it. This module selects the facts worth a
plan judgment, deterministically; the judgment itself is the same
surface-and-ask pass a plan-affecting note gets (`assess_note_impact`,
kind "fact") — the operator confirms before any step changes. Pure (regex)
so the ci-tier-0 gates can import it.
"""
from __future__ import annotations

import re
from typing import Any

# The shapes of a fact that can invalidate a step: a capability that is
# absent, gated, unsupported or forbidden, or a precondition the plan did not
# know about. A class of phrasings, not a product or a domain.
_BLOCKING_RE = re.compile(
    r"\b(?:not\s+available|no\s+option|there\s+is\s+no|there\s+are\s+no|does\s+not\s+(?:offer|support|allow|provide|include|exist)|"
    r"doesn'?t\s+(?:offer|support|allow|provide|include|exist)|cannot|can'?t\s+be|not\s+supported|unsupported|"
    r"requires?\s+(?:an?\s+)?(?:upgrade|paid|higher|different|separate)|upgrade\s+to|only\s+(?:available|on|in|with)\b|"
    r"must\s+(?:be|use|have)|never\s+(?:leases?|sees?|lists?)|is\s+not\s+(?:listed|visible|shown|reachable|installed)|"
    r"removed\s+in|deprecated|discontinued|greyed\s+out|grayed\s+out|disabled\s+on)\b",
    re.IGNORECASE)
_STOP = frozenset({"about", "after", "before", "their", "there", "these", "those", "which", "while", "would", "could",
                   "should", "where", "being", "other", "every", "under", "still", "using", "setup", "configure",
                   "verify", "confirm", "create", "install", "document", "validate", "check", "step", "operator"})


def _words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9-]{4,}", (t or "").lower()) if w not in _STOP}


def blocking_candidates(facts: list[str], pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``[{fact, node_keys, shared}]`` — each blocking-shaped fact with the
    pending steps it shares a DISCRIMINATING word with (a word in fewer than
    half the pending steps; "invoice" in one step of eleven picks it out,
    "quickbooks" in all of them does not)."""
    if not facts or not pending:
        return []
    node_words = [(str(n.get("node_key")), _words(f"{n.get('title') or ''} {n.get('prompt_template') or ''}")) for n in pending]
    freq: dict[str, int] = {}
    for _, ws in node_words:
        for w in ws:
            freq[w] = freq.get(w, 0) + 1
    half = max(1, len(node_words) / 2)
    out: list[dict[str, Any]] = []
    for f in facts:
        if not _BLOCKING_RE.search(f or ""):
            continue
        fw = _words(f)
        scored: list[tuple[int, str]] = []
        shared_all: set[str] = set()
        for nk, ws in node_words:
            shared = {w for w in (fw & ws) if freq.get(w, 0) < half or len(node_words) == 1}
            if shared:
                scored.append((len(shared), nk))
                shared_all |= shared
        if scored:
            # the steps sharing the MOST discriminating words lead the hint
            hits = [nk for _, nk in sorted(scored, key=lambda t: (-t[0], t[1]))]
            out.append({"fact": f, "node_keys": hits[:4], "shared": sorted(shared_all)[:6]})
    return out
