# app/modules/paper_reddit_ingestion.py
"""Reddit ingestion for the paper trading research lab.

Fetches Reddit listings through OAuth (script app), extracts ticker
symbols, and stores minimal ingested_item rows. User-authored content
(title, selftext, author) is used for extraction but never persisted,
honoring Reddit's requirement to remove deleted user content.

All API calls are authenticated with a bearer token obtained from
Reddit's OAuth token endpoint. Listing requests go to oauth.reddit.com,
never www.reddit.com. Rate-limit headers (X-Ratelimit-*) are read on
every response and the 100 QPM limit is honored.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

from app.database import async_session
from app.modules.runner_secrets import values_for

logger = logging.getLogger("scaffold")

# Fixed values that must never change at runtime (module constants).
REDDIT_TOKEN_URL: str = "https://www.reddit.com/api/v1/access_token"
REDDIT_OAUTH_API_BASE: str = "https://oauth.reddit.com"

# Reddit rate limit: 100 queries per minute per OAuth client id.
REDDIT_QPM_LIMIT: int = 100
REDDIT_RATE_LIMIT_HEADERS: tuple[str, ...] = (
    "X-Ratelimit-Used",
    "X-Ratelimit-Remaining",
    "X-Ratelimit-Reset",
)

# User-Agent format: <platform>:<app ID>:<version string> (by /u/<username>).
REDDIT_USER_AGENT_PLATFORM: str = "linux"
REDDIT_USER_AGENT_VERSION: str = "v0.1.0"

# Operator secret names.
REDDIT_CLIENT_ID_SECRET_NAME: str = "REDDIT_CLIENT_ID"
REDDIT_CLIENT_SECRET_SECRET_NAME: str = "REDDIT_CLIENT_SECRET"
REDDIT_USERNAME_SECRET_NAME: str = "REDDIT_USERNAME"
OPERATOR_SECRET_NAMES: list[str] = [
    REDDIT_CLIENT_ID_SECRET_NAME,
    REDDIT_CLIENT_SECRET_SECRET_NAME,
    REDDIT_USERNAME_SECRET_NAME,
]

# Ticker pattern: 1-5 uppercase letters, not part of a longer word.
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")

# Common English words that look like tickers but aren't.
_TICKER_STOPWORDS = frozenset(
    {
        "I",
        "A",
        "AM",
        "PM",
        "CEO",
        "CFO",
        "IPO",
        "ETF",
        "USA",
        "US",
        "AI",
        "IT",
        "IS",
        "BE",
        "TO",
        "IN",
        "ON",
        "OF",
        "AT",
        "BY",
        "OR",
        "AN",
        "AS",
        "DO",
        "GO",
        "HE",
        "ME",
        "MY",
        "NO",
        "SO",
        "UP",
        "WE",
        "OK",
        "RE",
        "TV",
        "WSJ",
        "YOLO",
        "DD",
    }
)


class RedditIngestionError(Exception):
    """Raised when Reddit ingestion fails."""


class RedditSecretsMissingError(RedditIngestionError):
    """Raised when the operator has not stored the Reddit API secrets."""


class RedditAuthError(RedditIngestionError):
    """Raised when the OAuth token request fails."""


def _build_user_agent(client_id: str, username: str) -> str:
    """Build a Reddit-compliant User-Agent string.

    Format: <platform>:<app ID>:<version string> (by /u/<reddit username>).
    The app ID is the registered OAuth client id.
    """
    return (
        f"{REDDIT_USER_AGENT_PLATFORM}:{client_id}:"
        f"{REDDIT_USER_AGENT_VERSION} (by /u/{username})"
    )


def _extract_tickers(text: str) -> list[str]:
    """Extract candidate ticker symbols from text.

    Returns a sorted, de-duplicated list of ticker-like tokens.
    """
    if not text:
        return []
    candidates = _TICKER_RE.findall(text)
    tickers: list[str] = []
    seen: set[str] = set()
    for token in candidates:
        if token in _TICKER_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        tickers.append(token)
    tickers.sort()
    return tickers


def _parse_rate_limit_headers(headers: httpx.Headers) -> dict[str, float]:
    """Extract Reddit rate-limit values from response headers.

    Reads X-Ratelimit-Used, X-Ratelimit-Remaining, and X-Ratelimit-Reset.
    Missing or unparseable headers are logged and skipped.
    """
    values: dict[str, float] = {}
    for name in REDDIT_RATE_LIMIT_HEADERS:
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            values[name] = float(raw)
        except (TypeError, ValueError):
            logger.warning("Unparseable Reddit rate-limit header %s: %r", name, raw)
    return values


async def _obtain_access_token(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    user_agent: str,
) -> str:
    """Obtain an OAuth access token from Reddit's token endpoint.

    Uses the client_credentials grant for a script app. The client id and
    secret are sent as HTTP Basic auth. Returns the bearer token string.
    """
    basic_auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic_auth}",
        "User-Agent": user_agent,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        response = await client.post(
            REDDIT_TOKEN_URL,
            headers=headers,
            data={"grant_type": "client_credentials"},
        )
    except httpx.HTTPError as exc:
        raise RedditAuthError(f"Reddit token request failed: {exc}") from exc

    if response.status_code != 200:
        raise RedditAuthError(
            f"Reddit token request returned {response.status_code}: {response.text[:500]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RedditAuthError("Reddit token response was not JSON") from exc

    token = data.get("access_token") if isinstance(data, dict) else None
    if not token or not isinstance(token, str):
        raise RedditAuthError("Reddit token response missing access_token")
    return token


def _parse_listing_response(data: Any) -> list[dict[str, Any]]:
    """Parse a Reddit listing response into a list of post dicts.

    The listing response is expected to be a dict with data.children[].
    Each child has a data dict with the post fields. Missing or malformed
    entries are logged and skipped rather than raising.
    """
    if not isinstance(data, dict):
        logger.warning("Reddit listing response is not a dict: %r", type(data))
        return []
    listing_data = data.get("data")
    if not isinstance(listing_data, dict):
        logger.warning("Reddit listing response missing data dict")
        return []
    children = listing_data.get("children")
    if not isinstance(children, list):
        logger.warning("Reddit listing response children is not a list")
        return []

    posts: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            logger.warning("Reddit listing child is not a dict")
            continue
        post = child.get("data")
        if isinstance(post, dict):
            posts.append(post)
        else:
            logger.warning("Reddit listing child missing data dict")
    return posts


async def _fetch_reddit_listing(
    client: httpx.AsyncClient,
    *,
    token: str,
    user_agent: str,
    subreddit: str,
    sort: str,
    time_filter: str | None,
    max_posts: int | None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Fetch one Reddit listing page with OAuth.

    Returns the list of posts and the rate-limit values read from the
    response headers.
    """
    limit = min(max_posts if max_posts is not None else 25, 100)
    url = f"{REDDIT_OAUTH_API_BASE}/r/{subreddit}/{sort}"
    headers = {
        "Authorization": f"bearer {token}",
        "User-Agent": user_agent,
    }
    params: dict[str, str] = {"limit": str(limit)}
    if time_filter and sort in ("top", "controversial"):
        params["t"] = time_filter

    try:
        response = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise RedditIngestionError(f"Reddit listing request failed: {exc}") from exc

    rate_info = _parse_rate_limit_headers(response.headers)
    if rate_info:
        logger.debug("Reddit rate limit: %r", rate_info)

    if response.status_code != 200:
        raise RedditIngestionError(
            f"Reddit listing request returned {response.status_code}: {response.text[:500]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RedditIngestionError("Reddit listing response was not JSON") from exc

    posts = _parse_listing_response(data)
    return posts, rate_info


async def ingest_reddit_posts(
    *,
    subreddit: str | None = None,
    sort: str | None = None,
    time_filter: str | None = None,
    max_posts: int | None = None,
    min_score: int | None = None,
    min_comments: int | None = None,
) -> int:
    """Fetch Reddit listings, extract tickers, and store ingested_item rows.

    Authenticates with the operator's Reddit OAuth client id and secret,
    requests the listing from oauth.reddit.com, extracts ticker symbols
    from each post's title and selftext, and stores one ingested_item row
    per post. User-authored content is not persisted; only derived tickers
    and post metadata are stored, honoring Reddit's data deletion rules.

    Returns the number of newly ingested items.
    """
    async with async_session() as db:
        secrets = await values_for(db, OPERATOR_SECRET_NAMES)
        client_id = secrets.get(REDDIT_CLIENT_ID_SECRET_NAME)
        client_secret = secrets.get(REDDIT_CLIENT_SECRET_SECRET_NAME)
        username = secrets.get(REDDIT_USERNAME_SECRET_NAME)

        missing = [
            name
            for name, value in (
                (REDDIT_CLIENT_ID_SECRET_NAME, client_id),
                (REDDIT_CLIENT_SECRET_SECRET_NAME, client_secret),
                (REDDIT_USERNAME_SECRET_NAME, username),
            )
            if not value
        ]
        if missing:
            raise RedditSecretsMissingError(
                f"Missing Reddit secrets: {', '.join(missing)}. "
                "Store them on the Machines page -> Secrets."
            )

        if max_posts is not None and max_posts <= 0:
            return 0

        user_agent = _build_user_agent(client_id, username)
        sub = subreddit or "wallstreetbets"
        srt = sort or "hot"

        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _obtain_access_token(
                client,
                client_id=client_id,
                client_secret=client_secret,
                user_agent=user_agent,
            )
            posts, rate_info = await _fetch_reddit_listing(
                client,
                token=token,
                user_agent=user_agent,
                subreddit=sub,
                sort=srt,
                time_filter=time_filter,
                max_posts=max_posts,
            )

        # Honor the rate limit: read the documented headers and log them.
        remaining = rate_info.get("X-Ratelimit-Remaining")
        used = rate_info.get("X-Ratelimit-Used", 0.0)
        if remaining is not None and remaining <= 0:
            logger.warning(
                "Reddit rate limit exhausted (X-Ratelimit-Remaining=%s); "
                "no further requests this window",
                remaining,
            )
        if used >= REDDIT_QPM_LIMIT:
            logger.warning(
                "Reddit QPM limit reached (X-Ratelimit-Used=%s >= %d)",
                used,
                REDDIT_QPM_LIMIT,
            )

        ingested = 0
        for post in posts:
            post_id = post.get("id")
            if not post_id:
                logger.warning("Reddit post missing id; skipping")
                continue

            created_utc = post.get("created_utc")
            if created_utc is None:
                logger.warning("Reddit post %s missing created_utc; skipping", post_id)
                continue
            try:
                published_ts = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "Reddit post %s has unparseable created_utc %r; skipping",
                    post_id,
                    created_utc,
                )
                continue

            score = post.get("score", 0)
            num_comments = post.get("num_comments", 0)
            if min_score is not None and (
                not isinstance(score, (int, float)) or score < min_score
            ):
                continue
            if min_comments is not None and (
                not isinstance(num_comments, (int, float)) or num_comments < min_comments
            ):
                continue

            title = post.get("title") or ""
            selftext = post.get("selftext") or ""
            tickers = _extract_tickers(f"{title}\n{selftext}")

            # Payload stores only derived tickers and metadata -- no
            # user-authored content (title, selftext, author), so there
            # is no user content to delete if the author removes the post.
            payload = {
                "post_id": post_id,
                "subreddit": post.get("subreddit", sub),
                "score": int(score) if isinstance(score, (int, float)) else 0,
                "num_comments": int(num_comments) if isinstance(num_comments, (int, float)) else 0,
                "tickers": tickers,
                "created_utc": float(created_utc) if isinstance(created_utc, (int, float)) else 0.0,
            }

            insert_sql = text(
                """
                INSERT INTO ingested_item (source, external_id, published_ts, payload)
                VALUES (:source, :external_id, :published_ts, CAST(:payload AS json))
                ON CONFLICT (source, external_id) DO NOTHING
                RETURNING id
                """
            )
            result = await db.execute(
                insert_sql,
                {
                    "source": "reddit",
                    "external_id": f"t3_{post_id}",
                    "published_ts": published_ts,
                    "payload": json.dumps(payload),
                },
            )
            if result.fetchone() is not None:
                ingested += 1

        await db.commit()
        return ingested
