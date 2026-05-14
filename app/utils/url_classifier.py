"""URL → source_type classifier for deep-search bypass.

Phase-1 producers (§17.106–§17.109) fetch curated content directly from
typed prefixes (``github:``, ``hf:``, ``so:``, etc.). URL mode + topic
mode, by contrast, fetch arbitrary URLs and run them through an LLM
distill step — which is wasted compute when the URL is a known producer
endpoint (stackoverflow.com/a/12345 is already an answer body).

This module classifies URLs by hostname + path so the URL-mode and
topic-mode flows can:
1. Tag the ingest entry with the right ``source_type`` (no need to
   ask the LLM to guess).
2. Optionally skip the distill step entirely (``should_distill``
   returns False for curated types).

§17.110 ships the classifier as infrastructure. The URL-mode + topic-mode
integration that consumes it lands in phase 2.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Curated source_types — content from these sources is already
# structured/distilled; the LLM extract pass adds nothing and burns 7b
# tokens. ``should_distill`` returns False for these.
CURATED_SOURCE_TYPES: frozenset[str] = frozenset({
    "release_notes",
    "test_code",
    "ci_config",
    "model_card",
    "dataset_card",
    "paper_abstract",
    "so_answer",
    "official_docs",
    "curated",
    # §17.166 — Wikipedia articles. The §17.112 docstring in
    # research_agent._run_research_url_mode explicitly lists Wikipedia
    # among the bypass-eligible source types, but ``wiki_article`` was
    # never actually added to this frozenset. Result: Wikipedia URLs
    # like https://en.wikipedia.org/wiki/Software_design_pattern fell
    # through to the LLM extract loop and burned ~7 min per batch on
    # this CPU host, exhausting curl ``--max-time 1800`` on multi-
    # batch pages and leaving the session row stuck in ``running``.
    # Direct chunk-to-entry path (no LLM) closes Wikipedia URLs in
    # seconds with no quality loss — Wikipedia content is already
    # structured, prose-clean, and trafilatura-extractable.
    "wiki_article",
})

# (host_pattern, path_pattern, source_type). First match wins.
_HOST_RULES: tuple[tuple[re.Pattern, re.Pattern, str], ...] = (
    # Stack Overflow — accepted answers + questions
    (re.compile(r"^(www\.)?stackoverflow\.com$", re.I),
     re.compile(r"^/a/\d+|^/questions/\d+"),
     "so_answer"),
    # Hacker News
    (re.compile(r"^news\.ycombinator\.com$", re.I),
     re.compile(r"^/item"),
     "hn_comment"),
    # Reddit posts
    (re.compile(r"^(www\.|old\.)?reddit\.com$", re.I),
     re.compile(r"^/r/[^/]+/comments/"),
     "reddit_post"),
    # arXiv
    (re.compile(r"^arxiv\.org$", re.I),
     re.compile(r"^/abs/|^/pdf/"),
     "paper_abstract"),
    # Hugging Face — papers
    (re.compile(r"^huggingface\.co$", re.I),
     re.compile(r"^/papers/"),
     "paper_abstract"),
    # Hugging Face — models / datasets / spaces (more specific paths first)
    (re.compile(r"^huggingface\.co$", re.I),
     re.compile(r"^/datasets/"),
     "dataset_card"),
    (re.compile(r"^huggingface\.co$", re.I),
     re.compile(r"^/spaces/"),
     "tech_docs"),
    (re.compile(r"^huggingface\.co$", re.I),
     re.compile(r"^/[^/]+/[^/]+/?$"),  # huggingface.co/<owner>/<repo>
     "model_card"),
    # Hugging Face docs
    (re.compile(r"^huggingface\.co$", re.I),
     re.compile(r"^/docs/"),
     "official_docs"),
    # Wikipedia (en + lang-prefixed)
    (re.compile(r"^[a-z]{2,3}\.wikipedia\.org$", re.I),
     re.compile(r"^/wiki/"),
     "wiki_article"),
    # GitHub — release pages
    (re.compile(r"^github\.com$", re.I),
     re.compile(r"^/[^/]+/[^/]+/releases"),
     "release_notes"),
    # GitHub — workflow files
    (re.compile(r"^github\.com$", re.I),
     re.compile(r"^/[^/]+/[^/]+/(blob|raw|tree)/[^/]+/\.github/workflows/"),
     "ci_config"),
    # GitHub — test files
    (re.compile(r"^github\.com$", re.I),
     re.compile(r"^/[^/]+/[^/]+/(blob|raw)/[^/]+/(tests?|spec)/"),
     "test_code"),
    # GitHub — issues / PRs (closed discussion threads)
    (re.compile(r"^github\.com$", re.I),
     re.compile(r"^/[^/]+/[^/]+/(issues|pull)/\d+"),
     "community"),
    # GitHub — anything else (README, docs, code)
    (re.compile(r"^github\.com$", re.I),
     re.compile(r"^/[^/]+/[^/]+"),
     "tech_docs"),
)


def classify_url(url: str) -> str | None:
    """Return the ``source_type`` matching ``url``, or ``None`` if unknown.

    Hostnames are normalized to lowercase; trailing slashes and queries
    don't affect classification. Returns ``None`` for URLs the rule
    table doesn't recognize — caller falls back to default behavior
    (e.g., LLM distill, ``source_type="tech_docs"``).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    path = parsed.path or "/"
    if not host:
        return None
    for host_re, path_re, source_type in _HOST_RULES:
        if host_re.match(host) and path_re.search(path):
            return source_type
    return None


def should_distill(source_type: str | None) -> bool:
    """True when the URL-mode / topic-mode pipeline should run the LLM
    distill pass on this content; False for curated source_types that
    are already structured.
    """
    if source_type is None:
        return True
    return source_type not in CURATED_SOURCE_TYPES
