"""Topic detection — score free-form text against topic keyword maps.

Shared by gt_extractor (GT pipeline) and ideation_workflow (Phase 2 GitHub push).
The algorithm lives here; keyword data stays with the caller so this module
has no domain coupling.

Example:
    >>> keywords = {1: ["llm", "prompt"], 2: ["rag", "retrieval"]}
    >>> detect_topic_id("building a RAG system", keywords)
    2
"""
from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _kw_pattern(kw_lower: str) -> re.Pattern[str]:
    """Compile a word-boundary pattern for ``kw_lower`` (already lowercased)."""
    return re.compile(rf"\b{re.escape(kw_lower)}\b")


def detect_topic_id(
    text: str,
    keywords_by_topic: dict[int, list[str]],
    default: int = 1,
) -> int:
    """Score ``text`` against each topic's keyword list; return highest scorer.

    Matching is case-insensitive and uses word boundaries, so ``"rag"`` no
    longer matches ``"storage"``. Caller-supplied keywords are lowercased
    defensively, so callers don't have to pre-normalize.

    Args:
        text: Free-form input to classify.
        keywords_by_topic: Mapping of topic_id -> list of keyword strings.
        default: Topic id returned when no keyword matches in any topic,
            or when ``keywords_by_topic`` is empty.

    Returns:
        The topic_id with the highest keyword-match count. When the highest
        score is zero (no matches anywhere), returns ``default``.
    """
    if not keywords_by_topic:
        return default
    lowered = text.lower()
    scores: dict[int, int] = {}
    for tid, kws in keywords_by_topic.items():
        score = 0
        for kw in kws:
            kw_lower = kw.lower().strip()
            if not kw_lower:
                continue
            if _kw_pattern(kw_lower).search(lowered):
                score += 1
        scores[tid] = score
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default
