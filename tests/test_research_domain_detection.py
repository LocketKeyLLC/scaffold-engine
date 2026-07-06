"""§17.501 — `_detect_domain` keyword-less fallback regression.

Pre-§17.501, `_detect_domain` passed ``default=1`` to ``detect_topic_id``,
so a research topic that matched NO keywords routed to topic_id 1 → the
"llm" Milvus partition — contradicting ``settings.default_domain`` ("eng")
and stranding e.g. homelab/infra research in "llm". The fix passes
``default=0`` (a topic_id absent from ``topic_to_domain``) so the
``.get(topic_id, settings.default_domain)`` fallback fires and unmatched
topics land in the documented default partition.
"""
from __future__ import annotations

from app.config import settings
from app.modules.research_extractors import _detect_domain


class TestDetectDomain:
    def test_keywordless_topic_falls_back_to_default_domain(self):
        # The exact topic that mis-routed: no TOPIC_KEYWORDS hit.
        assert _detect_domain(
            "HomeLab set ups, specifically control panels, media services"
        ) == settings.default_domain
        assert settings.default_domain == "eng"

    def test_unrelated_topic_is_not_llm(self):
        # Regression: keyword-less topics used to silently become "llm".
        assert _detect_domain("random gardening tips for beginners") == "eng"

    def test_matched_topics_still_route_correctly(self):
        assert _detect_domain("fine-tune an llm with rlhf") == "llm"
        assert _detect_domain("build a RAG retrieval pipeline") == "rag"
        assert _detect_domain("design a distributed microservice api") == "eng"
