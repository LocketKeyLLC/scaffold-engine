"""Tests for the §17.108 forum-mode helpers: SO, HN, arXiv parsers + fetchers."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_response(status_code: int = 200, json_data=None, content: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.content = content
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError
        resp.raise_for_status.side_effect = HTTPStatusError("err", request=None, response=resp)
    return resp


@pytest.fixture
def fake_cache_miss():
    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.put = AsyncMock(return_value=True)
    with patch("app.utils.forum_ingest.get_fetch_cache", return_value=cache):
        yield cache


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestForumParsers:
    def test_so_ref(self):
        from app.modules.research_extractors import _is_so_ref, _parse_so_ref
        assert _is_so_ref("so:list comprehension python")
        assert not _is_so_ref("so:")
        assert _parse_so_ref("so:foo bar") == "foo bar"

    def test_hn_ref(self):
        from app.modules.research_extractors import _is_hn_ref, _parse_hn_ref
        assert _is_hn_ref("hn:Show HN")
        assert _parse_hn_ref("hn: machine learning ") == "machine learning"

    def test_arxiv_ref_id(self):
        from app.modules.research_extractors import _is_arxiv_ref, _parse_arxiv_ref
        assert _is_arxiv_ref("arxiv:2310.06825")
        assert _parse_arxiv_ref("arxiv:2310.06825") == ("id", "2310.06825")
        assert _parse_arxiv_ref("arxiv:2310.06825v2") == ("id", "2310.06825v2")
        # Legacy arxiv format
        assert _parse_arxiv_ref("arxiv:cs.CL/0501001")[0] == "id"

    def test_arxiv_ref_query(self):
        from app.modules.research_extractors import _parse_arxiv_ref
        assert _parse_arxiv_ref("arxiv:transformer architecture") == ("query", "transformer architecture")
        assert _parse_arxiv_ref("arxiv:llm agents")[0] == "query"


# ---------------------------------------------------------------------------
# PII strip
# ---------------------------------------------------------------------------

class TestStripPii:
    def test_strips_username(self):
        from app.utils.forum_ingest import _strip_pii
        assert _strip_pii("hi @alice and @bob-1") == "hi @user and @user"

    def test_strips_email(self):
        from app.utils.forum_ingest import _strip_pii
        assert _strip_pii("contact me at jane@example.com") == "contact me at email@redacted"

    def test_preserves_normal_text(self):
        from app.utils.forum_ingest import _strip_pii
        assert _strip_pii("plain text no pii") == "plain text no pii"


class TestStripHtml:
    def test_drops_tags(self):
        from app.utils.forum_ingest import _strip_html
        assert _strip_html("<p>hello <b>world</b></p>") == "hello world"

    def test_decodes_entities(self):
        from app.utils.forum_ingest import _strip_html
        assert _strip_html("a &amp; b &lt; c") == "a & b < c"


# ---------------------------------------------------------------------------
# fetch_so_answers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_so_answers_accepted_passes_gate(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(side_effect=[
        # /search/advanced → 2 questions w/ accepted answers
        _make_response(json_data={"items": [
            {"question_id": 100, "title": "How do X?", "accepted_answer_id": 1001,
             "score": 50, "tags": ["python"]},
            {"question_id": 101, "title": "Why Y?", "accepted_answer_id": 1002,
             "score": 3, "tags": ["python"]},
        ]}),
        # /answers/{ids} batch → 1001 accepted (low score), 1002 not accepted (high score)
        _make_response(json_data={"items": [
            {"answer_id": 1001, "score": 2, "is_accepted": True,
             "body": "<p>Use foo(bar)</p>",
             "link": "https://stackoverflow.com/a/1001"},
            {"answer_id": 1002, "score": 25, "is_accepted": False,
             "body": "<p>Different approach @alice</p>",
             "link": "https://stackoverflow.com/a/1002"},
        ]}),
    ])
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_so_answers("foo", limit=10, min_score=10)

    refs = {e["source_ref"] for e in out}
    # 1001 passes via is_accepted=True (score 2 < 10); 1002 passes via score=25
    assert refs == {"answer-1001", "answer-1002"}
    # PII strip applied to 1002 body
    a1002 = next(e for e in out if e["source_ref"] == "answer-1002")
    assert "@alice" not in a1002["content"]
    assert "@user" in a1002["content"]


@pytest.mark.asyncio
async def test_fetch_so_answers_low_score_unaccepted_filtered(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(side_effect=[
        _make_response(json_data={"items": [
            {"question_id": 1, "title": "T", "accepted_answer_id": 1, "score": 1, "tags": []},
        ]}),
        _make_response(json_data={"items": [
            {"answer_id": 1, "score": 3, "is_accepted": False, "body": "<p>weak</p>",
             "link": "https://stackoverflow.com/a/1"},
        ]}),
    ])
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_so_answers("x", limit=10, min_score=10)
    assert out == []


@pytest.mark.asyncio
async def test_fetch_so_answers_uses_answer_cache():
    """Cached answer bodies skip the /answers/{ids} batch call entirely."""
    from app.utils import forum_ingest
    cached_body = json.dumps({
        "score": 30, "is_accepted": True,
        "body": "<p>cached body</p>",
        "link": "https://stackoverflow.com/a/777",
    }).encode("utf-8")

    cache = MagicMock()
    async def fake_get(stype, ref, path):
        if ref == "answer-777":
            return cached_body
        return None
    cache.get = AsyncMock(side_effect=fake_get)
    cache.put = AsyncMock(return_value=True)

    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(json_data={"items": [
        {"question_id": 1, "title": "Q", "accepted_answer_id": 777, "score": 5, "tags": []},
    ]}))

    with patch("app.utils.forum_ingest.get_fetch_cache", return_value=cache), \
         patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_so_answers("anything", limit=5, min_score=10)

    # Only ONE HTTP call (search), the answer body came from cache.
    assert client.get.call_count == 1
    assert len(out) == 1
    assert out[0]["source_ref"] == "answer-777"
    assert "cached body" in out[0]["content"]


@pytest.mark.asyncio
async def test_fetch_so_answers_429_returns_empty(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(status_code=429))
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_so_answers("x", limit=5, min_score=10)
    assert out == []


# ---------------------------------------------------------------------------
# fetch_hn_items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hn_items_min_points_gate(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(json_data={
        "hits": [
            {"objectID": "100", "title": "Big story", "points": 250,
             "story_text": "Hello @ada", "num_comments": 80,
             "created_at": "2025-01-01"},
            {"objectID": "101", "title": "Comment", "points": 5,
             "comment_text": "small", "num_comments": 0,
             "created_at": "2025-01-02"},
            {"objectID": "102", "title": None, "points": 120,
             "comment_text": "Detailed insight here.", "num_comments": 12,
             "created_at": "2025-01-03"},
        ],
    }))
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_hn_items("anything", limit=10, min_points=100)

    refs = {e["source_ref"] for e in out}
    assert refs == {"100", "102"}  # 101 filtered (points=5 < 100)

    by_ref = {e["source_ref"]: e for e in out}
    assert "@ada" not in by_ref["100"]["content"]
    assert by_ref["100"]["quality_signal"]["points"] == 250


@pytest.mark.asyncio
async def test_fetch_hn_items_zero_limit_short_circuits(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock()
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_hn_items("x", limit=0, min_points=10)
    assert out == []
    client.get.assert_not_called()


# ---------------------------------------------------------------------------
# fetch_arxiv
# ---------------------------------------------------------------------------

_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2310.06825v1</id>
    <title>A Sample Paper Title</title>
    <summary>This paper proposes a thing that does another thing well.</summary>
    <author><name>Alice Author</name></author>
    <author><name>Bob Coauthor</name></author>
    <published>2023-10-10T12:00:00Z</published>
  </entry>
</feed>"""


@pytest.mark.asyncio
async def test_fetch_arxiv_id_mode_parses_atom(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(content=_SAMPLE_ATOM.encode("utf-8")))

    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_arxiv("id", "2310.06825", limit=5)

    assert len(out) == 1
    e = out[0]
    assert e["source_type"] == "paper_abstract"
    assert e["source_ref"] == "2310.06825v1"
    assert "Alice Author, Bob Coauthor" in e["content"]
    assert "A Sample Paper Title" in e["content"]
    assert e["quality_signal"]["author_count"] == 2


@pytest.mark.asyncio
async def test_fetch_arxiv_id_mode_uses_cache_on_hit():
    from app.utils import forum_ingest
    cache = MagicMock()
    cache.get = AsyncMock(return_value=_SAMPLE_ATOM.encode("utf-8"))
    cache.put = AsyncMock(return_value=True)
    client = MagicMock()
    client.get = AsyncMock()
    with patch("app.utils.forum_ingest.get_fetch_cache", return_value=cache), \
         patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_arxiv("id", "2310.06825", limit=5)
    assert len(out) == 1
    client.get.assert_not_called()  # Cache hit short-circuits the network


@pytest.mark.asyncio
async def test_fetch_arxiv_zero_limit_short_circuits(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock()
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_arxiv("id", "anything", limit=0)
    assert out == []
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_arxiv_bad_mode_raises():
    from app.utils import forum_ingest
    with pytest.raises(ValueError, match="arxiv mode"):
        await forum_ingest.fetch_arxiv("nope", "value", limit=5)


# ---------------------------------------------------------------------------
# fetch_forum dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_forum_dispatches_by_prefix():
    from app.utils import forum_ingest
    with patch("app.utils.forum_ingest.fetch_so_answers",
               AsyncMock(return_value=[{"x": 1}])) as so, \
         patch("app.utils.forum_ingest.fetch_hn_items",
               AsyncMock(return_value=[{"x": 2}])) as hn, \
         patch("app.utils.forum_ingest.fetch_arxiv",
               AsyncMock(return_value=[{"x": 3}])) as ax:
        assert await forum_ingest.fetch_forum("so", "q") == [{"x": 1}]
        assert await forum_ingest.fetch_forum("hn", "q") == [{"x": 2}]
        assert await forum_ingest.fetch_forum("arxiv", "2310.06825") == [{"x": 3}]
        so.assert_awaited_once()
        hn.assert_awaited_once()
        ax.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_forum_unknown_prefix_raises():
    from app.utils import forum_ingest
    with pytest.raises(ValueError, match="Unknown forum prefix"):
        await forum_ingest.fetch_forum("twitter", "anything")


# ---------------------------------------------------------------------------
# Reddit (§17.109)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestRedditRefParser:
    def test_allowlist_accepts_canonical(self):
        from app.modules.research_extractors import _is_reddit_ref, _parse_reddit_ref
        assert _is_reddit_ref("reddit:MachineLearning:transformer attention")
        assert _parse_reddit_ref("reddit:MachineLearning:transformer attention") == (
            "MachineLearning", "transformer attention",
        )

    def test_allowlist_case_insensitive(self):
        from app.modules.research_extractors import _parse_reddit_ref
        # User-typed casing preserved in result, but allowlist match is case-insensitive.
        assert _parse_reddit_ref("reddit:machinelearning:foo") == ("machinelearning", "foo")
        assert _parse_reddit_ref("reddit:LOCALLLAMA:quantization") == (
            "LOCALLLAMA", "quantization",
        )

    def test_allowlist_rejects_unlisted(self):
        from app.modules.research_extractors import _parse_reddit_ref
        with pytest.raises(ValueError, match="not in allowlist"):
            _parse_reddit_ref("reddit:python:foo")
        with pytest.raises(ValueError, match="not in allowlist"):
            _parse_reddit_ref("reddit:programming:foo")

    def test_malformed_rejected(self):
        from app.modules.research_extractors import _parse_reddit_ref
        for bad in ["reddit:onlysubreddit", "reddit::query", "reddit:MachineLearning:"]:
            with pytest.raises(ValueError):
                _parse_reddit_ref(bad)


@pytest.mark.asyncio
async def test_fetch_reddit_posts_gates_score_and_comments(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(json_data={
        "data": {"children": [
            # Passes both gates
            {"data": {"id": "p1", "title": "Good post", "selftext": "Hi @ada body",
                      "score": 150, "num_comments": 30, "over_18": False,
                      "permalink": "/r/MachineLearning/comments/p1/", "created_utc": 1700000000}},
            # Score too low
            {"data": {"id": "p2", "title": "Mid post", "selftext": "x",
                      "score": 10, "num_comments": 30, "over_18": False,
                      "permalink": "/r/MachineLearning/comments/p2/", "created_utc": 0}},
            # NSFW filtered
            {"data": {"id": "p3", "title": "Bad", "selftext": "x",
                      "score": 999, "num_comments": 999, "over_18": True,
                      "permalink": "/r/MachineLearning/comments/p3/", "created_utc": 0}},
            # Link-only post (no selftext) → skipped
            {"data": {"id": "p4", "title": "Linkpost", "selftext": "",
                      "score": 999, "num_comments": 999, "over_18": False,
                      "url": "https://example.com",
                      "permalink": "/r/MachineLearning/comments/p4/", "created_utc": 0}},
            # Too few comments
            {"data": {"id": "p5", "title": "Low engagement", "selftext": "hi",
                      "score": 200, "num_comments": 5, "over_18": False,
                      "permalink": "/r/MachineLearning/comments/p5/", "created_utc": 0}},
        ]},
    }))
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_reddit_posts(
            "MachineLearning", "anything", limit=10,
            min_score=50, min_comments=10,
        )

    refs = {e["source_ref"] for e in out}
    assert refs == {"t3_p1"}
    p1 = out[0]
    # PII strip applied
    assert "@ada" not in p1["content"]
    assert "@user" in p1["content"]
    # source_url stitches together base + permalink
    assert p1["source_url"].endswith("/r/MachineLearning/comments/p1/")
    assert p1["source_type"] == "reddit_post"
    assert p1["quality_signal"]["score"] == 150
    assert p1["quality_signal"]["num_comments"] == 30


@pytest.mark.asyncio
async def test_fetch_reddit_posts_429_returns_empty(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(status_code=429))
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_reddit_posts(
            "MachineLearning", "x", limit=5, min_score=50, min_comments=10,
        )
    assert out == []


@pytest.mark.asyncio
async def test_fetch_reddit_posts_zero_limit_short_circuits(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock()
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_reddit_posts(
            "MachineLearning", "x", limit=0, min_score=50, min_comments=10,
        )
    assert out == []
    client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Wikipedia (§17.109)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestWikiRefParser:
    def test_parses_topic(self):
        from app.modules.research_extractors import _is_wiki_ref, _parse_wiki_ref
        assert _is_wiki_ref("wiki:Transformer (machine learning model)")
        assert _parse_wiki_ref("wiki:Transformer (machine learning model)") == (
            "Transformer (machine learning model)"
        )

    def test_empty_rejected(self):
        from app.modules.research_extractors import _parse_wiki_ref
        with pytest.raises(ValueError):
            _parse_wiki_ref("wiki:")
        with pytest.raises(ValueError):
            _parse_wiki_ref("wiki:   ")


@pytest.mark.asyncio
async def test_fetch_wiki_pages_two_step_search_then_extract(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(side_effect=[
        # Step 1: search returns 2 titles
        _make_response(json_data={
            "query": {"search": [
                {"title": "Transformer (machine learning model)"},
                {"title": "Attention (machine learning)"},
            ]},
        }),
        # Step 2: extract returns plain text + revid
        _make_response(json_data={
            "query": {"pages": [
                {"title": "Transformer (machine learning model)",
                 "extract": "Lorem ipsum about transformers.",
                 "lastrevid": 12345},
                {"title": "Attention (machine learning)",
                 "extract": "Attention is a mechanism...",
                 "lastrevid": 67890},
            ]},
        }),
    ])
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_wiki_pages("transformer attention", limit=5)

    assert len(out) == 2
    by_title = {e["path"]: e for e in out}
    t = by_title["wiki/Transformer (machine learning model)"]
    assert t["source_type"] == "wiki_article"
    assert t["source_ref"] == "12345"
    assert t["source_url"].endswith("Transformer_(machine_learning_model)")
    assert "Lorem ipsum" in t["content"]


@pytest.mark.asyncio
async def test_fetch_wiki_pages_skips_empty_extracts(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock(side_effect=[
        _make_response(json_data={"query": {"search": [{"title": "Foo"}, {"title": "Bar"}]}}),
        _make_response(json_data={"query": {"pages": [
            {"title": "Foo", "extract": "", "lastrevid": 1},      # empty → skip
            {"title": "Bar", "extract": "content", "lastrevid": 2},
        ]}}),
    ])
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_wiki_pages("x", limit=5)
    assert [e["path"] for e in out] == ["wiki/Bar"]


@pytest.mark.asyncio
async def test_fetch_wiki_pages_zero_limit_short_circuits(fake_cache_miss):
    from app.utils import forum_ingest
    client = MagicMock()
    client.get = AsyncMock()
    with patch("app.utils.forum_ingest.get_generic_http_client", return_value=client):
        out = await forum_ingest.fetch_wiki_pages("x", limit=0)
    assert out == []
    client.get.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch — reddit + wiki
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_forum_dispatches_reddit_and_wiki():
    from app.utils import forum_ingest
    with patch("app.utils.forum_ingest.fetch_reddit_posts",
               AsyncMock(return_value=[{"x": 9}])) as rd, \
         patch("app.utils.forum_ingest.fetch_wiki_pages",
               AsyncMock(return_value=[{"x": 10}])) as wk:
        assert await forum_ingest.fetch_forum("reddit", "MachineLearning:foo") == [{"x": 9}]
        assert await forum_ingest.fetch_forum("wiki", "transformer") == [{"x": 10}]
        rd.assert_awaited_once_with(
            "MachineLearning", "foo",
            forum_ingest.settings.reddit_max_posts,
            forum_ingest.settings.reddit_min_score,
            forum_ingest.settings.reddit_min_comments,
        )
        wk.assert_awaited_once_with("transformer", forum_ingest.settings.wiki_max_pages)
