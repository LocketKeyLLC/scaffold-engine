"""Behavioral tests for app/utils/github_ingest."""
import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _disable_tree_cache():
    """The pre-M6 tests assume no caching layer. Audit M6 added one, so
    short-circuit `_redis_client()` to None for these tests; the new
    cache-specific tests in test_github_ingest_cache.py opt back in by
    patching at a different level.
    """
    with patch("app.utils.github_ingest._redis_client", AsyncMock(return_value=None)):
        yield


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


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


@pytest.mark.asyncio
async def test_fetch_repo_content_happy_path():
    """README + one docs/*.md + one top-level *.py with docstring."""
    from app.utils import github_ingest

    responses = [
        # /repos/{owner}/{repo} -> default_branch
        _make_response(json_data={"default_branch": "main"}),
        # /repos/{owner}/{repo}/readme
        _make_response(json_data={"path": "README.md", "content": _b64("# Hello\nreadme body")}),
        # /repos/{owner}/{repo}/git/trees/main?recursive=1
        _make_response(json_data={"tree": [
            {"path": "docs/guide.md", "type": "blob", "sha": "sha1"},
            {"path": "main.py", "type": "blob", "sha": "sha2"},
            {"path": "nested/skip.md", "type": "blob", "sha": "sha3"},
            {"path": "skip.txt", "type": "blob", "sha": "sha4"},
        ]}),
        # Blob for docs/guide.md
        _make_response(json_data={"encoding": "base64", "content": _b64("# Guide\nbody")}),
        # Blob for main.py
        _make_response(json_data={"encoding": "base64", "content": _b64('"""Main module docstring."""\nprint("hi")')}),
    ]

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=responses)

    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        results = await github_ingest.fetch_repo_content("owner", "repo")

    paths = [r["path"] for r in results]
    assert "README.md" in paths
    assert "docs/guide.md" in paths
    assert "main.py" in paths
    assert "nested/skip.md" not in paths  # not a top-level docs entry
    assert "skip.txt" not in paths  # wrong extension

    # Python entry should contain only the docstring, not the print statement
    py_entry = next(r for r in results if r["path"] == "main.py")
    assert py_entry["content"] == "Main module docstring."


@pytest.mark.asyncio
async def test_repo_not_found_raises():
    """404 on /repos/{owner}/{repo} raises GitHubRepoNotFoundError."""
    from app.utils import github_ingest

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(status_code=404))

    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        with pytest.raises(github_ingest.GitHubRepoNotFoundError):
            await github_ingest.fetch_repo_content("missing", "repo")


@pytest.mark.asyncio
async def test_rate_limit_exhausted_raises():
    """X-RateLimit-Remaining=0 raises GitHubRateLimitError."""
    from app.utils import github_ingest

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(
        json_data={"default_branch": "main"},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1234567890"},
    ))

    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        with pytest.raises(github_ingest.GitHubRateLimitError):
            await github_ingest.fetch_repo_content("owner", "repo")


@pytest.mark.asyncio
async def test_file_cap_enforced(monkeypatch):
    """Over-cap selection is truncated."""
    from app.utils import github_ingest
    from app.config import settings

    monkeypatch.setattr(settings, "github_max_files", 3)

    # 5 markdown files; cap=3 leaves 2 after README
    tree_entries = [
        {"path": f"docs/f{i}.md", "type": "blob", "sha": f"sha{i}"}
        for i in range(5)
    ]
    responses = [
        _make_response(json_data={"default_branch": "main"}),
        _make_response(json_data={"path": "README.md", "content": _b64("# R")}),
        _make_response(json_data={"tree": tree_entries}),
    ]
    # 2 blob fetches (cap - 1 README = 2 remaining)
    responses += [
        _make_response(json_data={"encoding": "base64", "content": _b64(f"body{i}")})
        for i in range(2)
    ]

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=responses)

    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        results = await github_ingest.fetch_repo_content("owner", "repo")

    assert len(results) == 3  # README + 2 docs


@pytest.mark.asyncio
async def test_python_no_docstring_skipped():
    """Top-level *.py without docstring is excluded."""
    from app.utils import github_ingest

    responses = [
        _make_response(json_data={"default_branch": "main"}),
        _make_response(status_code=404),  # no README
        _make_response(json_data={"tree": [
            {"path": "nodoc.py", "type": "blob", "sha": "shaX"},
        ]}),
        _make_response(json_data={"encoding": "base64", "content": _b64("print('hi')\n")}),
    ]

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=responses)

    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        results = await github_ingest.fetch_repo_content("owner", "repo")

    assert results == []


def test_is_github_ref_matches_valid():
    from app.modules.research_agent import _is_github_ref
    assert _is_github_ref("github:owner/repo")
    assert _is_github_ref("github:anthropics/claude-code")


def test_is_github_ref_rejects_invalid():
    from app.modules.research_agent import _is_github_ref
    assert not _is_github_ref("https://github.com/owner/repo")
    assert not _is_github_ref("github:owner")
    assert not _is_github_ref("github:owner/repo/extra")
    assert not _is_github_ref("github:.com/repo")  # owner with dot
    assert not _is_github_ref("regular topic")


def test_parse_github_ref():
    from app.modules.research_agent import _parse_github_ref
    assert _parse_github_ref("github:owner/repo") == ("owner", "repo")
    assert _parse_github_ref("github:  owner/repo  ") == ("owner", "repo")
