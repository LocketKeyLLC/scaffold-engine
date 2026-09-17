"""§17.1087 — a pasted SCREEN and the fact it must yield. Pure (regex only)
so the ci-tier-0 gates can import it without the database driver.
"""
from __future__ import annotations

import re

# A pasted SCREEN: many short lines, no shell prompts, no command
# echoes, almost no sentences. Structural, not a vocabulary — a router page, an
# installer menu and an app screen all look like this and a command paste never
# does. `surface_fact_present` is the check on the scribe's output: a fact
# about a surface names the surface class (page/screen/app/console/portal/
# menu/tab/settings) — a class of nouns, like the hardware nouns, not a product.
_PROMPT_LINE_RE = re.compile(r"^\S+@\S+[:~][^$#\n]*[$#]\s|^\s*(?:\$|#)\s+\S", re.MULTILINE)
_SURFACE_NOUN_RE = re.compile(
    r"\b(?:page|screen|app|application|console|portal|dashboard|menu|tab|panel|web ui|web interface|"
    r"settings|wizard|installer)\b", re.IGNORECASE)


def looks_like_screen_paste(text: str) -> bool:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) < 6 or _PROMPT_LINE_RE.search(text or ""):
        return False
    short = sum(1 for ln in lines if len(ln.split()) <= 6)
    sentences = sum(1 for ln in lines if ln.strip().endswith((".", "?", "!")) and len(ln.split()) > 8)
    return short >= len(lines) * 0.6 and sentences <= max(2, len(lines) // 5)


def surface_fact_present(fact: str) -> bool:
    return bool(_SURFACE_NOUN_RE.search(fact or ""))


