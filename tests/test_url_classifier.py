"""Tests for app.utils.url_classifier — URL → source_type classification."""
from __future__ import annotations

import pytest

from app.utils.url_classifier import (
    CURATED_SOURCE_TYPES,
    classify_url,
    should_distill,
)


@pytest.mark.smoke
class TestClassifyUrl:
    @pytest.mark.parametrize("url,expected", [
        # Stack Overflow
        ("https://stackoverflow.com/a/12345", "so_answer"),
        ("https://stackoverflow.com/questions/100/why", "so_answer"),
        ("https://www.stackoverflow.com/a/100", "so_answer"),
        # Hacker News
        ("https://news.ycombinator.com/item?id=12345", "hn_comment"),
        # Reddit
        ("https://reddit.com/r/MachineLearning/comments/abc/title/", "reddit_post"),
        ("https://www.reddit.com/r/LocalLLaMA/comments/xyz/", "reddit_post"),
        ("https://old.reddit.com/r/Python/comments/foo/", "reddit_post"),
        # arXiv
        ("https://arxiv.org/abs/2310.06825", "paper_abstract"),
        ("https://arxiv.org/pdf/2310.06825", "paper_abstract"),
        # Hugging Face — papers
        ("https://huggingface.co/papers/2310.06825", "paper_abstract"),
        # Hugging Face — datasets
        ("https://huggingface.co/datasets/squad", "dataset_card"),
        ("https://huggingface.co/datasets/openai/gsm8k", "dataset_card"),
        # Hugging Face — spaces
        ("https://huggingface.co/spaces/owner/demo", "tech_docs"),
        # Hugging Face — models
        ("https://huggingface.co/microsoft/phi-2", "model_card"),
        ("https://huggingface.co/meta-llama/Llama-3-8B", "model_card"),
        # Hugging Face — docs
        ("https://huggingface.co/docs/transformers/installation", "official_docs"),
        # Wikipedia
        ("https://en.wikipedia.org/wiki/Transformer", "wiki_article"),
        ("https://fr.wikipedia.org/wiki/Transformer", "wiki_article"),
        # GitHub — releases / workflows / tests / issues
        ("https://github.com/owner/repo/releases/tag/v1.0", "release_notes"),
        ("https://github.com/owner/repo/blob/main/.github/workflows/ci.yml", "ci_config"),
        ("https://github.com/owner/repo/raw/main/tests/test_x.py", "test_code"),
        ("https://github.com/owner/repo/blob/main/spec/foo.py", "test_code"),
        ("https://github.com/owner/repo/issues/42", "community"),
        ("https://github.com/owner/repo/pull/99", "community"),
        # GitHub — fallback tech_docs
        ("https://github.com/owner/repo", "tech_docs"),
        ("https://github.com/owner/repo/blob/main/README.md", "tech_docs"),
    ])
    def test_classification(self, url, expected):
        assert classify_url(url) == expected

    @pytest.mark.parametrize("url", [
        "https://example.com/foo",
        "https://random-blog.io/post",
        "",
        "not a url at all",
        "ftp://something/path",
    ])
    def test_unknown_returns_none(self, url):
        assert classify_url(url) is None

    def test_case_insensitive_host(self):
        # Hostnames matched case-insensitively
        assert classify_url("https://STACKOVERFLOW.COM/a/1") == "so_answer"
        assert classify_url("https://HuggingFace.co/microsoft/phi-2") == "model_card"


@pytest.mark.smoke
class TestShouldDistill:
    @pytest.mark.parametrize("source_type", sorted(CURATED_SOURCE_TYPES))
    def test_curated_skips_distill(self, source_type):
        assert should_distill(source_type) is False

    @pytest.mark.parametrize("source_type", [
        "community",       # forum threads — distill helps extract takeaways
        "wiki_article",    # mutable, paraphrased
        "hn_comment",      # not in curated set
        "reddit_post",     # not in curated set
        "tech_docs",       # README-style prose
        "ai_generated",
        "news",
        "real_time",
    ])
    def test_uncurated_runs_distill(self, source_type):
        assert should_distill(source_type) is True

    def test_none_runs_distill(self):
        # Unknown URLs (classify_url returned None) → caller doesn't have
        # a basis to skip; default to running the distill pass.
        assert should_distill(None) is True
