# tests/test_paper_reddit_ingestion.py
"""Tests for paper_reddit_ingestion."""

import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_reddit_ingestion


class FakeResponse:
    """Minimal httpx.Response stand-in for mocked network calls."""

    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.headers = httpx.Headers(headers or {})
        self.text = text

    def json(self):
        return self._json_data


def _token_response(access_token="test-token"):
    return FakeResponse(
        status_code=200,
        json_data={"access_token": access_token, "token_type": "bearer"},
    )


def _listing_response(posts=None, *, headers=None, status_code=200, text=""):
    if posts is None:
        posts = []
    return FakeResponse(
        status_code=status_code,
        json_data={"kind": "Listing", "data": {"children": posts}},
        headers=headers or {"X-Ratelimit-Used": "1", "X-Ratelimit-Remaining": "99", "X-Ratelimit-Reset": "600"},
        text=text,
    )


def _post(
    post_id,
    *,
    created_utc=1700000000.0,
    title="",
    selftext="",
    subreddit="wallstreetbets",
    score=100,
    num_comments=50,
):
    return {
        "kind": "t3",
        "data": {
            "id": post_id,
            "subreddit": subreddit,
            "created_utc": created_utc,
            "title": title,
            "selftext": selftext,
            "author": "someuser",
            "score": score,
            "num_comments": num_comments,
        },
    }


def _secrets():
    return {
        "REDDIT_CLIENT_ID": "client-1",
        "REDDIT_CLIENT_SECRET": "secret-1",
        "REDDIT_USERNAME": "testuser",
    }


def _run_with_mocks(token_response=None, listing_response=None, secrets=None):
    """Patch values_for and httpx.AsyncClient, return the fake client."""
    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=token_response or _token_response())
    fake_client.get = AsyncMock(return_value=listing_response or _listing_response())
    patchers = [
        patch("app.modules.paper_reddit_ingestion.values_for", AsyncMock(return_value=secrets or _secrets())),
        patch("app.modules.paper_reddit_ingestion.httpx.AsyncClient", return_value=fake_client),
    ]
    return fake_client, patchers


@pytest.mark.asyncio
async def test_missing_secrets_raises():
    with patch(
        "app.modules.paper_reddit_ingestion.values_for",
        AsyncMock(return_value={}),
    ):
        with pytest.raises(paper_reddit_ingestion.RedditSecretsMissingError):
            await paper_reddit_ingestion.ingest_reddit_posts()


@pytest.mark.asyncio
async def test_ingests_posts_with_oauth_and_extracts_tickers():
    listing = _listing_response(
        [
            _post(
                "post123",
                title="AAPL is going to the moon",
                selftext="TSLA also looks good",
            )
        ]
    )
    fake_client, patchers = _run_with_mocks(listing_response=listing)
    with patchers[0], patchers[1]:
        count = await paper_reddit_ingestion.ingest_reddit_posts(
            subreddit="wallstreetbets", sort="hot"
        )

    assert count == 1

    # The listing request must go to oauth.reddit.com.
    listing_url = fake_client.get.call_args.args[0]
    assert listing_url == "https://oauth.reddit.com/r/wallstreetbets/hot"

    # The token request must use www.reddit.com (token endpoint).
    token_url = fake_client.post.call_args.args[0]
    assert token_url == "https://www.reddit.com/api/v1/access_token"

    async with async_session() as db:
        rows = (
            await db.execute(
                text("SELECT source, external_id, published_ts, payload FROM ingested_item")
            )
        ).fetchall()
    assert len(rows) == 1
    source, external_id, published_ts, payload = rows[0]
    assert source == "reddit"
    assert external_id == "t3_post123"
    assert published_ts == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert payload["tickers"] == ["AAPL", "TSLA"]


@pytest.mark.asyncio
async def test_user_agent_format():
    fake_client, patchers = _run_with_mocks()
    with patchers[0], patchers[1]:
        await paper_reddit_ingestion.ingest_reddit_posts()

    expected_ua = "linux:client-1:v0.1.0 (by /u/testuser)"
    token_headers = fake_client.post.call_args.kwargs["headers"]
    listing_headers = fake_client.get.call_args.kwargs["headers"]
    assert token_headers["User-Agent"] == expected_ua
    assert listing_headers["User-Agent"] == expected_ua


@pytest.mark.asyncio
async def test_oauth_bearer_token_sent():
    fake_client, patchers = _run_with_mocks(token_response=_token_response("tok-abc"))
    with patchers[0], patchers[1]:
        await paper_reddit_ingestion.ingest_reddit_posts()

    listing_headers = fake_client.get.call_args.kwargs["headers"]
    assert listing_headers["Authorization"] == "bearer tok-abc"


@pytest.mark.asyncio
async def test_rate_limit_headers_read(caplog):
    listing = _listing_response(
        headers={"X-Ratelimit-Used": "100", "X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": "60"}
    )
    fake_client, patchers = _run_with_mocks(listing_response=listing)
    with patchers[0], patchers[1], caplog.at_level(logging.WARNING, logger="scaffold"):
        await paper_reddit_ingestion.ingest_reddit_posts()

    assert any("X-Ratelimit-Remaining=0" in r.message for r in caplog.records)
    assert any("QPM limit reached" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_duplicate_post_skipped():
    post_id = uuid4().hex
    # Pre-insert the same external_id the module will try to insert.
    async with async_session() as db:
        await db.execute(
            text(
                """
                INSERT INTO ingested_item (source, external_id, published_ts, payload)
                VALUES ('reddit', :external_id, :published_ts, CAST(:payload AS json))
                """
            ),
            {
                "external_id": f"t3_{post_id}",
                "published_ts": datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc),
                "payload": json.dumps({"post_id": post_id, "tickers": ["AAPL"]}),
            },
        )
        await db.commit()

    listing = _listing_response([_post(post_id, title="AAPL moon")])
    fake_client, patchers = _run_with_mocks(listing_response=listing)
    with patchers[0], patchers[1]:
        count = await paper_reddit_ingestion.ingest_reddit_posts()

    assert count == 0
    async with async_session() as db:
        total = (await db.execute(text("SELECT count(*) FROM ingested_item"))).scalar()
    assert total == 1


@pytest.mark.asyncio
async def test_min_score_filter():
    listing = _listing_response(
        [
            _post("p1", score=500),
            _post("p2", score=10),
        ]
    )
    fake_client, patchers = _run_with_mocks(listing_response=listing)
    with patchers[0], patchers[1]:
        count = await paper_reddit_ingestion.ingest_reddit_posts(min_score=100)

    assert count == 1
    async with async_session() as db:
        rows = (await db.execute(text("SELECT external_id FROM ingested_item"))).fetchall()
    assert [r[0] for r in rows] == ["t3_p1"]


@pytest.mark.asyncio
async def test_min_comments_filter():
    listing = _listing_response(
        [
            _post("p1", num_comments=200),
            _post("p2", num_comments=5),
        ]
    )
    fake_client, patchers = _run_with_mocks(listing_response=listing)
    with patchers[0], patchers[1]:
        count = await paper_reddit_ingestion.ingest_reddit_posts(min_comments=50)

    assert count == 1
    async with async_session() as db:
        rows = (await db.execute(text("SELECT external_id FROM ingested_item"))).fetchall()
    assert [r[0] for r in rows] == ["t3_p1"]


@pytest.mark.asyncio
async def test_payload_excludes_user_content():
    listing = _listing_response(
        [_post("p1", title="AAPL moon", selftext="Buy the dip")]
    )
    fake_client, patchers = _run_with_mocks(listing_response=listing)
    with patchers[0], patchers[1]:
        await paper_reddit_ingestion.ingest_reddit_posts()

    async with async_session() as db:
        payload = (await db.execute(text("SELECT payload FROM ingested_item"))).scalar()
    assert "title" not in payload
    assert "selftext" not in payload
    assert "author" not in payload
    assert payload["tickers"] == ["AAPL"]


@pytest.mark.asyncio
async def test_max_posts_capped_at_100():
    fake_client, patchers = _run_with_mocks()
    with patchers[0], patchers[1]:
        await paper_reddit_ingestion.ingest_reddit_posts(max_posts=500)

    params = fake_client.get.call_args.kwargs["params"]
    assert params["limit"] == "100"


@pytest.mark.asyncio
async def test_auth_error_raises():
    token_response = FakeResponse(status_code=401, json_data={}, text="Unauthorized")
    fake_client, patchers = _run_with_mocks(token_response=token_response)
    with patchers[0], patchers[1]:
        with pytest.raises(paper_reddit_ingestion.RedditAuthError):
            await paper_reddit_ingestion.ingest_reddit_posts()


@pytest.mark.asyncio
async def test_listing_error_raises():
    listing = FakeResponse(status_code=500, json_data={}, text="Server error")
    fake_client, patchers = _run_with_mocks(listing_response=listing)
    with patchers[0], patchers[1]:
        with pytest.raises(paper_reddit_ingestion.RedditIngestionError):
            await paper_reddit_ingestion.ingest_reddit_posts()
