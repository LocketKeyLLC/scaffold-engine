"""§17.292 — `push_to_github` returns structured `{category, detail, reason}` on failure.

§17.280-UX-6 audit-tail concern: every failure path in
``gt_extractor.push_to_github`` returned a free-text ``reason`` string.
A UI could not dispatch retry behavior by failure KIND (rate-limit
backoff vs auth re-prompt vs not-found path-edit) without regex-
parsing the reason text — fragile, and brittle to drift in the error
strings.

§17.292 promotes the failure shape to ``{pushed: False, category,
detail, reason}`` with a closed string-set for ``category``:

    config | auth | not_found | rate_limit | server | network | unknown

``reason`` is preserved verbatim for backward compatibility with
consumers that haven't migrated; new consumers dispatch off
``category``.

These tests pin every category mapping at the boundary so a future
classifier change is visible in test review.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.modules import gt_extractor
from app.modules.gt_extractor import (
    _PUSH_CATEGORY_AUTH,
    _PUSH_CATEGORY_CONFIG,
    _PUSH_CATEGORY_NETWORK,
    _PUSH_CATEGORY_NOT_FOUND,
    _PUSH_CATEGORY_RATE_LIMIT,
    _PUSH_CATEGORY_SERVER,
    _PUSH_CATEGORY_UNKNOWN,
    _push_category_for_exception,
    _push_category_for_status,
    _push_failure,
    push_to_github,
)
from app.utils.github_ingest import GitHubRateLimitError, GitHubRepoNotFoundError


# ---------------------------------------------------------------------------
# Unit tests for the classifier helpers (the dispatch core).
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestStatusClassifier:
    """§17.292 — HTTP status → category mapping."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_codes_map_to_auth(self, status):
        assert _push_category_for_status(status) == _PUSH_CATEGORY_AUTH

    def test_404_maps_to_not_found(self):
        assert _push_category_for_status(404) == _PUSH_CATEGORY_NOT_FOUND

    def test_429_maps_to_rate_limit(self):
        assert _push_category_for_status(429) == _PUSH_CATEGORY_RATE_LIMIT

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_5xx_maps_to_server(self, status):
        assert _push_category_for_status(status) == _PUSH_CATEGORY_SERVER

    @pytest.mark.parametrize("status", [400, 410, 418, 422])
    def test_other_4xx_maps_to_unknown(self, status):
        """Pin the catch-all — anything outside the named buckets falls
        through to `unknown`, NOT to a heuristic guess."""
        assert _push_category_for_status(status) == _PUSH_CATEGORY_UNKNOWN


@pytest.mark.smoke
class TestExceptionClassifier:
    """§17.292 — exception type → category mapping."""

    def test_connect_error_maps_to_network(self):
        exc = httpx.ConnectError("connection refused")
        assert _push_category_for_exception(exc) == _PUSH_CATEGORY_NETWORK

    def test_read_timeout_maps_to_network(self):
        exc = httpx.ReadTimeout("read timed out")
        assert _push_category_for_exception(exc) == _PUSH_CATEGORY_NETWORK

    def test_value_error_maps_to_unknown(self):
        """Non-network, non-categorized exception → unknown (catch-all)."""
        assert _push_category_for_exception(ValueError("boom")) == _PUSH_CATEGORY_UNKNOWN

    def test_classifier_uses_class_name_not_isinstance(self):
        """Pin the §17.292 substring-on-classname heuristic — chosen
        so we don't have to import every httpx/socket type up at the
        gt_extractor module level. A future refactor that flips to
        isinstance checks should keep these tests green."""

        class FakeNetworkTimeout(Exception):
            pass

        assert (
            _push_category_for_exception(FakeNetworkTimeout("fake"))
            == _PUSH_CATEGORY_NETWORK
        )


@pytest.mark.smoke
class TestPushFailureShape:
    """§17.292 — `_push_failure` builds the canonical response dict."""

    def test_returns_pushed_false(self):
        d = _push_failure(_PUSH_CATEGORY_AUTH, "401 Unauthorized")
        assert d["pushed"] is False

    def test_carries_category_and_detail(self):
        d = _push_failure(_PUSH_CATEGORY_NOT_FOUND, "owner/repo missing")
        assert d["category"] == _PUSH_CATEGORY_NOT_FOUND
        assert d["detail"] == "owner/repo missing"

    def test_default_reason_includes_category_prefix(self):
        """When reason isn't supplied, default to `"{category}: {detail}"`
        — that keeps the pre-§17.292 legacy reason string readable for
        backward compatibility."""
        d = _push_failure(_PUSH_CATEGORY_RATE_LIMIT, "exhausted")
        assert d["reason"] == "rate_limit: exhausted"

    def test_explicit_reason_overrides_default(self):
        """Existing callers that pass `reason=` keep their string
        verbatim — important for log-grep continuity."""
        d = _push_failure(
            _PUSH_CATEGORY_CONFIG,
            "github_token not set",
            reason="github_token not set in settings",
        )
        assert d["reason"] == "github_token not set in settings"


# ---------------------------------------------------------------------------
# End-to-end behavioral tests — drive push_to_github with each kind of
# failure and assert the category.
# ---------------------------------------------------------------------------


@pytest.fixture
def _force_token(monkeypatch):
    """The first early-return checks settings.github_token; set it for
    tests that drive deeper failure paths."""
    monkeypatch.setattr(gt_extractor.settings, "github_token", "test-token")
    monkeypatch.setattr(gt_extractor.settings, "gt_github_owner", "o")
    monkeypatch.setattr(gt_extractor.settings, "gt_github_repo", "r")
    monkeypatch.setattr(gt_extractor.settings, "gt_github_branch", "main")


def _resp(status: int, body: dict | None = None) -> MagicMock:
    """A mock httpx-like Response with status_code + json + raise_for_status."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status}", request=MagicMock(), response=MagicMock(status_code=status),
        )
    else:
        r.raise_for_status.return_value = None
    return r


@pytest.mark.asyncio
class TestPushToGithubFailureCategories:
    """§17.292 — push_to_github routes every failure through _push_failure."""

    async def test_missing_token_returns_config_category(self, monkeypatch):
        monkeypatch.setattr(gt_extractor.settings, "github_token", "")
        out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out == {
            "pushed": False,
            "category": "config",
            "detail": "github_token not set in settings",
            "reason": "github_token not set in settings",
        }

    async def test_get_404_uses_not_found_branch_NOT_failure(self, _force_token):
        """Reading the existing file 404 is success — the function treats
        it as "new file, fresh header". Pin that this is NOT a failure
        path. (The non-404 / non-200 case below tests the failure
        branch.)"""
        # GET /rate_limit succeeds; GET file 404 → new-file path;
        # then ref/heads/main fetched for SHA; create_ref returns 201;
        # branch_file_sha SELECT 404; PUT contents returns 201; PR
        # post returns 201.
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            _resp(200, {"resources": {"core": {"remaining": 5000}}}),  # /rate_limit
            _resp(404),                                                # GET existing file
            _resp(200, {"object": {"sha": "deadbeef"}}),               # /git/ref/heads/main
            _resp(404),                                                 # GET file on new branch
        ])
        client.post = AsyncMock(side_effect=[
            _resp(201),                                                 # POST /git/refs
            _resp(201, {"number": 7, "html_url": "https://pr"}),        # POST /pulls
        ])
        client.put = AsyncMock(return_value=_resp(201))                # PUT contents
        with patch.object(gt_extractor, "get_github_client", return_value=client), \
             patch.object(gt_extractor, "check_github_rate_limit", lambda r: None):
            out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out["pushed"] is True
        assert out.get("pr_number") == 7

    async def test_get_503_returns_server_category(self, _force_token):
        """503 (or any 5xx) on the initial file fetch → server category."""
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            _resp(200, {"resources": {"core": {"remaining": 5000}}}),
            _resp(503),
        ])
        with patch.object(gt_extractor, "get_github_client", return_value=client), \
             patch.object(gt_extractor, "check_github_rate_limit", lambda r: None):
            out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out["category"] == _PUSH_CATEGORY_SERVER
        assert out["detail"].startswith("GitHub GET failed:")
        # Reason preserved for backward compat.
        assert "GitHub GET failed: 503" in out["reason"]

    async def test_get_401_returns_auth_category(self, _force_token):
        """401 Unauthorized on the file fetch → auth category."""
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[
            _resp(200, {"resources": {"core": {"remaining": 5000}}}),
            _resp(401),
        ])
        with patch.object(gt_extractor, "get_github_client", return_value=client), \
             patch.object(gt_extractor, "check_github_rate_limit", lambda r: None):
            out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out["category"] == _PUSH_CATEGORY_AUTH

    async def test_rate_limit_error_returns_rate_limit_category(self, _force_token):
        """GitHubRateLimitError raised mid-flight → rate_limit category,
        reason preserved as `"rate_limit: <msg>"` for log-grep continuity.
        """
        client = AsyncMock()
        client.get = AsyncMock(side_effect=GitHubRateLimitError("quota exhausted"))
        with patch.object(gt_extractor, "get_github_client", return_value=client):
            out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out["category"] == _PUSH_CATEGORY_RATE_LIMIT
        assert "rate_limit:" in out["reason"]
        assert "quota exhausted" in out["detail"]

    async def test_repo_not_found_returns_not_found_category(self, _force_token):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=GitHubRepoNotFoundError("o/r not found"))
        with patch.object(gt_extractor, "get_github_client", return_value=client):
            out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out["category"] == _PUSH_CATEGORY_NOT_FOUND
        assert "not_found:" in out["reason"]

    async def test_network_error_returns_network_category(self, _force_token):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch.object(gt_extractor, "get_github_client", return_value=client):
            out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out["category"] == _PUSH_CATEGORY_NETWORK

    async def test_unknown_exception_returns_unknown_category(self, _force_token):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=ValueError("synthetic"))
        with patch.object(gt_extractor, "get_github_client", return_value=client):
            out = await push_to_github(rows=["row1"], file_path="k/x.toon", topic="x")
        assert out["category"] == _PUSH_CATEGORY_UNKNOWN


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.292 — anchor production source so a drive-by refactor that
    drops the category dispatch fails review."""

    def test_push_failure_helper_anchored(self):
        with open(gt_extractor.__file__, encoding="utf-8") as f:
            src = f.read()

        # The category constants form a closed set; pin every member
        # so a future addition forces a test + audit update.
        for c in ("config", "auth", "not_found", "rate_limit", "server",
                  "network", "unknown"):
            assert f'_PUSH_CATEGORY_{c.upper()} = "{c}"' in src, (
                f"§17.292 regression: category constant for {c!r} is "
                "missing. The closed string-set was the audit-fix's "
                "load-bearing invariant — removing a category breaks "
                "downstream UIs that dispatch on it."
            )

    def test_legacy_reason_string_only_returns_removed(self):
        """Pre-§17.292 the failure dicts were `{"pushed": False, "reason":
        ...}` — just two keys. The audit fix added `category` + `detail`
        on every path. A regression that re-introduces a bare
        `{"pushed": False, "reason": ...}` literal would slip through
        behavioural tests if the rest of the dict shape happens to be
        ignored by callers."""
        with open(gt_extractor.__file__, encoding="utf-8") as f:
            src = f.read()

        # The exact pre-§17.292 dict literal pattern.
        bare_pattern = '{"pushed": False, "reason":'
        assert bare_pattern not in src, (
            "§17.292 regression: a bare `{'pushed': False, 'reason': ...}` "
            "literal has reappeared in gt_extractor.py. The audit-fix "
            "requires every failure path to route through `_push_failure` "
            "so `category` is always set."
        )
