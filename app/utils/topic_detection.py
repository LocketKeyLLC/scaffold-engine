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


def detect_topic_id(
    text: str,
    keywords_by_topic: dict[int, list[str]],
    default: int = 1,
) -> int:
    """Score ``text`` against each topic's keyword list; return highest scorer.

    Args:
        text: Free-form input to classify. Matched case-insensitively.
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
    scores = {
        tid: sum(1 for kw in kws if kw in lowered)
        for tid, kws in keywords_by_topic.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default
