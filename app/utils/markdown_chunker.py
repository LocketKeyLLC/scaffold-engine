"""Split markdown into separate ``(chunk, kind)`` entries where kind is
``"code"`` or ``"prose"``.

Background: a README typically interleaves prose explanations with
fenced code blocks. Embedding the whole file as one chunk dilutes both
neighborhoods — the code-search-intent query (§17.118) won't preferentially
match a chunk where 80% of the text is English. Splitting on triple-
backtick fences gives the embedder cleaner per-chunk topical signal.

Applied to GH READMEs / CHANGELOGs / issue+PR bodies, HF model+dataset
cards. Forum content (SO/HN/Reddit) is already HTML-flattened with
``<pre>`` tags stripped by §17.108's ``_strip_html`` — no fences to
split — so this helper is a no-op there.

Caller convention: every returned chunk gets the ``kind`` appended to
its entry's ``domain_tags`` so retrieval can filter ``where domain_tags
includes 'code'``.
"""
from __future__ import annotations

import re

# Triple-backtick fenced code block. ``re.DOTALL`` lets ``.*?`` cross
# newlines; ``^``/``$`` are anchored per-line via ``re.MULTILINE``.
# The opening fence may have a language tag (``\`\`\`python``); we drop it.
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def split_markdown_by_kind(text: str) -> list[tuple[str, str]]:
    """Split ``text`` into a list of ``(chunk, kind)`` tuples.

    - ``kind == "code"`` for the body of each fenced code block.
    - ``kind == "prose"`` for everything outside fences.
    - Whitespace-only segments dropped.
    - Input with no fences returns ``[(text.strip(), "prose")]`` —
      single-tuple result, caller can detect "no split happened" by
      ``len(result) == 1``.
    - Empty / whitespace-only input returns ``[]``.
    """
    if not text or not text.strip():
        return []

    result: list[tuple[str, str]] = []
    last_end = 0
    for m in _FENCE_RE.finditer(text):
        # Prose before this fence (if any).
        prose = text[last_end:m.start()].strip()
        if prose:
            result.append((prose, "prose"))
        code = m.group(1).strip()
        if code:
            result.append((code, "code"))
        last_end = m.end()

    tail = text[last_end:].strip()
    if tail:
        result.append((tail, "prose"))

    if not result:
        # All-whitespace between/after fences but a fence existed
        # somewhere — fall back to a single prose chunk so the caller
        # always gets at least one tuple for non-empty input.
        result = [(text.strip(), "prose")]
    return result
