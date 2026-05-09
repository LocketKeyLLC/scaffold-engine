"""Audit M6 — Redis tree cache for github_ingest._get_tree.

Closes #151. Verifies the cache flow:

  - Miss → live API call → ETag-bearing response cached
  - Hit + 304 → cached blobs + truncated returned, no body parse
  - Hit + 200 → new response parsed, cache rewritten
  - TTL=0 disables the cache entirely
  - Redis init failure fails-open to a normal live call

The Redis client is replaced with an in-memory fake so tests don't need
a live Redis instance. The httpx client is the same MagicMock pattern
the existing test_github_ingest.py uses.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils import github_ingest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRedis:
    """Minimal in-memory async Redis stand-in for cache tests."""

    def __init__(self, initial: dict | None = None):
        self.store: dict[str, str] = dict(initial or {})
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple] = []  # (op, key, ...) for assertions

    async def get(self, key: str):
        self.calls.append(("get", key))
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.calls.append(("set", key, value, ex))
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def expire(self, key: str, ttl: int):
        self.calls.append(("expire", key, ttl))
        if key in self.store:
            self.ttls[key] = ttl


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _make_response(status_code=200, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {"X-RateLimit-Remaining": "4999"}
    resp.json = MagicMock(return_value=json_data or {})
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError
        resp.raise_for_status.side_effect = HTTPStatusError("err", request=None, response=resp)
    return resp


def _full_repo_responses(tree_resp):
    """The 3 prefix calls every fetch_repo_content makes before the tree call,
    plus the caller-provided tree response. README has no extra blobs to fetch
    because we omit it via the 404 path; tree has no blobs to make this small.
    """
    return [
        _make_response(json_data={"default_branch": "main"}),  # default branch
        _make_response(status_code=404),                       # no README
        tree_resp,                                              # tree
    ]


@pytest.fixture
def fake_redis():
    """Provide a fresh _FakeRedis and patch it into github_ingest._redis_client."""
    fake = _FakeRedis()
    with patch("app.utils.github_ingest._redis_client", AsyncMock(return_value=fake)):
        yield fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestTreeCacheMissThenWrite:
    async def test_first_call_writes_etag_blobs_truncated(self, fake_redis):
        """Cache miss → live API call → response with etag is cached."""
        tree_resp = _make_response(
            json_data={"tree": [{"path": "main.py", "type": "blob", "sha": "sha2"}]},
            headers={"X-RateLimit-Remaining": "4999", "etag": 'W/"abc123"'},
        )
        # main.py needs a docstring blob fetch to land in results.
        blob_resp = _make_response(
            json_data={"encoding": "base64", "content": _b64('"""hi"""\n')},
        )
        responses = _full_repo_responses(tree_resp) + [blob_resp]
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=responses)

        with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
            await github_ingest.fetch_repo_content("owner", "repo")

        # Cache GET fired (miss), then SET fired with the response shape.
        ops = [c[0] for c in fake_redis.calls]
        assert "get" in ops, f"expected cache GET; saw: {ops}"
        assert "set" in ops, f"expected cache SET; saw: {ops}"

        set_call = next(c for c in fake_redis.calls if c[0] == "set")
        _, key, value, ex = set_call
        assert key == "github:tree:v1:owner/repo:main"
        payload = json.loads(value)
        assert payload["etag"] == 'W/"abc123"'
        assert payload["truncated"] is False
        assert payload["blobs"] == [{"path": "main.py", "type": "blob", "sha": "sha2"}]
        assert ex == 1800  # default TTL


@pytest.mark.smoke
class TestTreeCacheHit304:
    async def test_304_returns_cached_blobs_no_reparse(self, fake_redis):
        """Cache hit + 304 → cached blobs returned, body not parsed,
        TTL refreshed."""
        cached_blobs = [{"path": "docs/guide.md", "type": "blob", "sha": "shaG"}]
        fake_redis.store["github:tree:v1:owner/repo:main"] = json.dumps({
            "etag": 'W/"abc123"',
            "blobs": cached_blobs,
            "truncated": False,
        })

        tree_304 = _make_response(status_code=304, headers={
            "X-RateLimit-Remaining": "4999",
            "etag": 'W/"abc123"',
        })
        # The 304 short-circuit means no .json() / no blob fetches happen
        # for the tree itself. We DO fetch the cached docs/guide.md blob.
        blob_resp = _make_response(
            json_data={"encoding": "base64", "content": _b64("# Guide")},
        )
        responses = _full_repo_responses(tree_304) + [blob_resp]
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=responses)

        with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
            results = await github_ingest.fetch_repo_content("owner", "repo")

        # The cached path made it into results.
        assert any(r["path"] == "docs/guide.md" for r in results)

        # If-None-Match was sent on the tree call.
        tree_call = mock_client.get.await_args_list[2]
        sent_headers = tree_call.kwargs.get("headers") or {}
        assert sent_headers.get("If-None-Match") == 'W/"abc123"'

        # 304 path doesn't write a new entry but DOES refresh TTL.
        ops = [c[0] for c in fake_redis.calls]
        assert "expire" in ops
        assert "set" not in ops
        # Tree response body was never parsed.
        tree_304.json.assert_not_called()


@pytest.mark.smoke
class TestTreeCacheHit200:
    async def test_invalidated_response_overwrites_cache(self, fake_redis):
        """Cache hit + 200 → cache rewritten with new etag + new blobs."""
        fake_redis.store["github:tree:v1:owner/repo:main"] = json.dumps({
            "etag": 'W/"old"',
            "blobs": [{"path": "old.py", "type": "blob", "sha": "old-sha"}],
            "truncated": False,
        })

        tree_resp = _make_response(
            json_data={"tree": [{"path": "new.py", "type": "blob", "sha": "new-sha"}]},
            headers={"X-RateLimit-Remaining": "4999", "etag": 'W/"new"'},
        )
        blob_resp = _make_response(
            json_data={"encoding": "base64", "content": _b64('"""new"""\n')},
        )
        responses = _full_repo_responses(tree_resp) + [blob_resp]
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=responses)

        with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
            await github_ingest.fetch_repo_content("owner", "repo")

        # Cache was rewritten with the new payload.
        new_value = fake_redis.store["github:tree:v1:owner/repo:main"]
        new_payload = json.loads(new_value)
        assert new_payload["etag"] == 'W/"new"'
        assert new_payload["blobs"] == [{"path": "new.py", "type": "blob", "sha": "new-sha"}]


@pytest.mark.smoke
class TestTreeCacheDisabled:
    async def test_ttl_zero_skips_redis(self, monkeypatch):
        """github_tree_cache_ttl_seconds=0 means _redis_client returns None
        → no Redis ops at all."""
        from app.config import settings
        monkeypatch.setattr(settings, "github_tree_cache_ttl_seconds", 0)

        # Reset any prior module-level _redis to make sure init is exercised.
        monkeypatch.setattr(github_ingest, "_redis", None)

        tree_resp = _make_response(
            json_data={"tree": []},
            headers={"X-RateLimit-Remaining": "4999", "etag": 'W/"x"'},
        )
        responses = _full_repo_responses(tree_resp)
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=responses)

        # No Redis patch — if the code ever tried to talk to Redis, this
        # would attempt a real connection (and either succeed or warn).
        # The real assertion below: _redis_client() returns None when TTL=0,
        # so the cache helpers no-op.
        client = await github_ingest._redis_client()
        assert client is None

        with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
            await github_ingest.fetch_repo_content("owner", "repo")
        # No assertion error → the live path completed without crashing.


@pytest.mark.smoke
class TestTreeCacheFailOpen:
    async def test_redis_get_failure_falls_through(self, monkeypatch):
        """Redis GET error → cache treated as miss, live call still works."""
        broken = _FakeRedis()

        async def _boom_get(_key):
            raise RuntimeError("simulated redis failure")
        broken.get = _boom_get  # override the method

        with patch("app.utils.github_ingest._redis_client", AsyncMock(return_value=broken)):
            tree_resp = _make_response(
                json_data={"tree": []},
                headers={"X-RateLimit-Remaining": "4999"},  # no etag
            )
            responses = _full_repo_responses(tree_resp)
            mock_client = MagicMock()
            mock_client.get = AsyncMock(side_effect=responses)

            with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
                # Should not raise — fail-open semantics.
                results = await github_ingest.fetch_repo_content("owner", "repo")

            assert results == []  # empty tree, no README


@pytest.mark.smoke
class TestTreeCacheBranchKeying:
    async def test_different_branches_dont_collide(self, fake_redis):
        """Cache keys include the branch — distinct branches use distinct keys."""
        # Pre-populate a 'main' entry. Now fetch from 'develop' — should miss.
        fake_redis.store["github:tree:v1:owner/repo:main"] = json.dumps({
            "etag": 'W/"main"', "blobs": [], "truncated": False,
        })

        tree_resp = _make_response(
            json_data={"tree": []},
            headers={"X-RateLimit-Remaining": "4999", "etag": 'W/"develop"'},
        )
        responses = [
            _make_response(json_data={"default_branch": "develop"}),
            _make_response(status_code=404),  # no README
            tree_resp,
        ]
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=responses)

        with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
            await github_ingest.fetch_repo_content("owner", "repo")

        # 'develop' was a miss — no If-None-Match should have been sent.
        tree_call = mock_client.get.await_args_list[2]
        sent_headers = tree_call.kwargs.get("headers") or {}
        assert "If-None-Match" not in sent_headers
        # And the develop key was written.
        assert "github:tree:v1:owner/repo:develop" in fake_redis.store
        # The main key is untouched.
        main_payload = json.loads(fake_redis.store["github:tree:v1:owner/repo:main"])
        assert main_payload["etag"] == 'W/"main"'
