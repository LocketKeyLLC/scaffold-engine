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
