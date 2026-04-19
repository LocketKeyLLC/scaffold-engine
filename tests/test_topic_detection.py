"""Unit tests for app.utils.topic_detection.detect_topic_id."""
import pytest
from app.utils.topic_detection import detect_topic_id


KEYWORDS = {
    1: ["llm", "prompt", "model"],
    2: ["rag", "retrieval", "vector"],
    3: ["spec", "openapi", "swagger"],
    4: ["code", "refactor", "debug"],
    5: ["deploy", "docker", "kubernetes"],
}


@pytest.mark.smoke
class TestDetectTopicId:
    """Score free-form text, pick highest-matching topic."""

    def test_llm_topic(self):
        assert detect_topic_id("optimize this prompt for the llm", KEYWORDS) == 1

    def test_rag_topic(self):
        assert detect_topic_id("build a RAG retrieval pipeline", KEYWORDS) == 2

    def test_spec_topic(self):
        assert detect_topic_id("generate openapi swagger docs", KEYWORDS) == 3

    def test_code_topic(self):
        assert detect_topic_id("refactor this code please", KEYWORDS) == 4

    def test_deploy_topic(self):
        assert detect_topic_id("deploy with docker compose", KEYWORDS) == 5

    def test_unknown_returns_default(self):
        assert detect_topic_id("completely unrelated text", KEYWORDS) == 1

    def test_empty_map_returns_default(self):
        assert detect_topic_id("anything", {}, default=42) == 42

    def test_case_insensitive(self):
        assert detect_topic_id("BUILD A RAG SYSTEM", KEYWORDS) == 2
