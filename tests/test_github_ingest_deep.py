"""Tests for the §17.106 GitHub deep-mode extensions:

- ``_classify_path`` heuristic (filename → source_type)
- ``_select_tree_files`` picks tests/*.py + .github/workflows/*.yml
- ``_resolve_ref_to_sha`` (commit-resolution path)
- ``fetch_repo_content(..., ref_hint=...)`` (pinned-ref behavior)
- ``fetch_repo_releases`` (release-notes endpoint)
- ``fetch_repo_issues_and_prs`` (closed-thread reaction gate)
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers (mirror tests/test_github_ingest.py conventions)
# ---------------------------------------------------------------------------

def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _make_response(status_code: int = 200, json_data=None, headers=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {"X-RateLimit-Remaining": "4999"}
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError
        resp.raise_for_status.side_effect = HTTPStatusError("err", request=None, response=resp)
    return resp


# ---------------------------------------------------------------------------
# _classify_path
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestClassifyPath:
    @pytest.mark.parametrize("path,expected", [
        # release_notes — exact basename match, case-insensitive
        ("CHANGELOG.md", "release_notes"),
        ("changelog.md", "release_notes"),
        ("docs/CHANGELOG.md", "release_notes"),  # nested still matches
        ("RELEASES.md", "release_notes"),
        ("HISTORY.md", "release_notes"),
        ("CHANGES.md", "release_notes"),
        ("NEWS.md", "release_notes"),
        # ci_config
        (".github/workflows/ci.yml", "ci_config"),
        (".github/workflows/release.yaml", "ci_config"),
        # test_code — must be .py AND under tests/|test/|spec/
        ("tests/test_foo.py", "test_code"),
        ("test/test_bar.py", "test_code"),
        ("spec/baz_spec.py", "test_code"),
        # tech_docs — fallback
        ("README.md", "tech_docs"),
        ("docs/guide.md", "tech_docs"),
        ("main.py", "tech_docs"),
        ("foo/bar.md", "tech_docs"),  # md outside docs/ + root still tech_docs (selector excludes)
    ])
    def test_classification(self, path, expected):
        from app.utils.github_ingest import _classify_path
        assert _classify_path(path) == expected

    def test_release_notes_wins_over_tech_docs(self):
        # CHANGELOG inside docs/ should NOT be tech_docs.
        from app.utils.github_ingest import _classify_path
        assert _classify_path("docs/CHANGELOG.md") == "release_notes"


# ---------------------------------------------------------------------------
# _select_tree_files
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestSelectTreeFiles:
    def test_picks_tests_one_level(self):
        from app.utils.github_ingest import _select_tree_files
        tree = [
            {"path": "tests/test_a.py", "type": "blob", "sha": "a"},
            {"path": "tests/sub/test_deep.py", "type": "blob", "sha": "b"},
            {"path": "test/test_b.py", "type": "blob", "sha": "c"},
            {"path": "spec/test_c.py", "type": "blob", "sha": "d"},
        ]
        paths = [e["path"] for e in _select_tree_files(tree, 100)]
        assert "tests/test_a.py" in paths
        assert "test/test_b.py" in paths
        assert "spec/test_c.py" in paths
        # deeper than one level → excluded
        assert "tests/sub/test_deep.py" not in paths

    def test_picks_workflows(self):
        from app.utils.github_ingest import _select_tree_files
        tree = [
            {"path": ".github/workflows/ci.yml", "type": "blob", "sha": "a"},
            {"path": ".github/workflows/release.yaml", "type": "blob", "sha": "b"},
            {"path": ".github/dependabot.yml", "type": "blob", "sha": "c"},  # NOT a workflow
        ]
        paths = [e["path"] for e in _select_tree_files(tree, 100)]
        assert ".github/workflows/ci.yml" in paths
        assert ".github/workflows/release.yaml" in paths
        assert ".github/dependabot.yml" not in paths

    def test_existing_behavior_preserved(self):
        # Original selector picked docs/**/*.md, top-level *.md, top-level *.py.
        from app.utils.github_ingest import _select_tree_files
        tree = [
            {"path": "README.md", "type": "blob", "sha": "a"},
            {"path": "docs/guide.md", "type": "blob", "sha": "b"},
            {"path": "main.py", "type": "blob", "sha": "c"},
            {"path": "src/internal.py", "type": "blob", "sha": "d"},  # excluded
        ]
        paths = [e["path"] for e in _select_tree_files(tree, 100)]
        assert "README.md" in paths
        assert "docs/guide.md" in paths
        assert "main.py" in paths
        assert "src/internal.py" not in paths


# ---------------------------------------------------------------------------
# _resolve_ref_to_sha
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_ref_to_sha_returns_sha():
    from app.utils import github_ingest
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(
        json_data={"sha": "abc123def456789"},
    ))
    sha = await github_ingest._resolve_ref_to_sha(client, "owner", "repo", "v1.2.3")
    assert sha == "abc123def456789"
    client.get.assert_called_with("/repos/owner/repo/commits/v1.2.3")


@pytest.mark.asyncio
async def test_resolve_ref_to_sha_404_raises_not_found():
    from app.utils import github_ingest
    client = MagicMock()
    client.get = AsyncMock(return_value=_make_response(status_code=404))
    with pytest.raises(github_ingest.GitHubRepoNotFoundError, match="@nope"):
        await github_ingest._resolve_ref_to_sha(client, "owner", "repo", "nope")


# ---------------------------------------------------------------------------
# fetch_repo_content with ref_hint — pinned path resolves SHA
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_repo_content_with_ref_resolves_sha_and_pins():
    from app.utils import github_ingest

    # Blob fetch order matches _select_tree_files: docs(.md) + pyfiles
    # + test_py + ci_yaml. With this tree that's README.md, CHANGELOG.md,
    # tests/test_x.py, .github/workflows/ci.yml.
    responses = [
        # commits/{ref_hint} → SHA
        _make_response(json_data={"sha": "shabeef" + "0" * 33}),
        # README (404 path — tree-based README ingest takes over)
        _make_response(status_code=404),
        # tree at sha
        _make_response(json_data={"tree": [
            {"path": "README.md", "type": "blob", "sha": "blobRA"},
            {"path": "tests/test_x.py", "type": "blob", "sha": "blobTX"},
            {"path": ".github/workflows/ci.yml", "type": "blob", "sha": "blobCI"},
            {"path": "CHANGELOG.md", "type": "blob", "sha": "blobCL"},
        ]}, headers={"X-RateLimit-Remaining": "4999"}),
        # blobs in selector order
        _make_response(json_data={"encoding": "base64", "content": _b64("# README\n")}),
        _make_response(json_data={"encoding": "base64", "content": _b64("# Changelog\n## 1.0\n")}),
        _make_response(json_data={"encoding": "base64", "content": _b64('"""Tests for x."""\n')}),
        _make_response(json_data={"encoding": "base64", "content": _b64("name: ci\non: push\n")}),
    ]
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=responses)

    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        results = await github_ingest.fetch_repo_content("owner", "repo", ref_hint="v1.2.3")

    by_path = {r["path"]: r for r in results}
    # All entries pinned to resolved SHA in source_ref
    for r in results:
        assert r["source_ref"] == "shabeef" + "0" * 33
        assert "blob/" + ("shabeef" + "0" * 33) in r["source_url"]
    # Source-type classification
    assert by_path["README.md"]["source_type"] == "tech_docs"
    assert by_path["tests/test_x.py"]["source_type"] == "test_code"
    assert by_path[".github/workflows/ci.yml"]["source_type"] == "ci_config"
    assert by_path["CHANGELOG.md"]["source_type"] == "release_notes"


# ---------------------------------------------------------------------------
# fetch_repo_releases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_repo_releases_happy_path():
    from app.utils import github_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(json_data=[
        {"tag_name": "v2.0.0", "name": "2.0", "body": "Big release notes",
         "html_url": "https://github.com/o/r/releases/tag/v2.0.0",
         "draft": False, "prerelease": False, "published_at": "2026-05-01T00:00:00Z"},
        {"tag_name": "v1.9.0", "name": "", "body": "Minor",
         "html_url": "https://github.com/o/r/releases/tag/v1.9.0",
         "draft": False, "prerelease": False, "published_at": "2026-04-01T00:00:00Z"},
    ]))
    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        rs = await github_ingest.fetch_repo_releases("o", "r", limit=5)
    assert len(rs) == 2
    assert rs[0]["source_type"] == "release_notes"
    assert rs[0]["source_ref"] == "v2.0.0"
    assert "Big release notes" in rs[0]["content"]


@pytest.mark.asyncio
async def test_fetch_repo_releases_skips_drafts_and_bodyless():
    from app.utils import github_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(json_data=[
        {"tag_name": "v1", "name": "1", "body": "", "draft": False,  # bodyless
         "html_url": "x", "prerelease": False, "published_at": ""},
        {"tag_name": "v2", "name": "2", "body": "real", "draft": True,  # draft
         "html_url": "x", "prerelease": False, "published_at": ""},
        {"tag_name": "v3", "name": "3", "body": "kept", "draft": False,
         "html_url": "x", "prerelease": False, "published_at": ""},
    ]))
    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        rs = await github_ingest.fetch_repo_releases("o", "r", limit=10)
    assert [r["source_ref"] for r in rs] == ["v3"]


@pytest.mark.asyncio
async def test_fetch_repo_releases_zero_limit_short_circuits():
    from app.utils import github_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        rs = await github_ingest.fetch_repo_releases("o", "r", limit=0)
    assert rs == []
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_repo_releases_404_returns_empty():
    from app.utils import github_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(status_code=404))
    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        rs = await github_ingest.fetch_repo_releases("o", "r", limit=5)
    assert rs == []


# ---------------------------------------------------------------------------
# fetch_repo_issues_and_prs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_repo_issues_and_prs_reaction_gate():
    from app.utils import github_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(json_data=[
        # Gated out: 0 positive reactions
        {"number": 1, "title": "low", "body": "x",
         "reactions": {"+1": 0, "heart": 0, "hooray": 0},
         "state": "closed", "closed_at": "", "html_url": ""},
        # Issue, passes gate (5 +1)
        {"number": 42, "title": "popular issue", "body": "details",
         "reactions": {"+1": 5, "heart": 0, "hooray": 0},
         "state": "closed", "closed_at": "2026-04-15T00:00:00Z",
         "html_url": "https://github.com/o/r/issues/42"},
        # PR, passes gate (heart counts)
        {"number": 99, "title": "fix", "body": "patch",
         "reactions": {"+1": 0, "heart": 3, "hooray": 0},
         "pull_request": {"url": "..."},
         "state": "closed", "closed_at": "2026-05-01T00:00:00Z",
         "html_url": "https://github.com/o/r/pull/99"},
    ]))
    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        items = await github_ingest.fetch_repo_issues_and_prs(
            "o", "r", limit=10, min_reactions=2,
        )
    by_ref = {it["source_ref"]: it for it in items}
    assert "issue-42" in by_ref
    assert "pr-99" in by_ref
    assert "issue-1" not in by_ref
    assert by_ref["issue-42"]["quality_signal"]["positive_reactions"] == 5
    assert by_ref["pr-99"]["quality_signal"]["kind"] == "pr"
    assert by_ref["issue-42"]["source_type"] == "community"


@pytest.mark.asyncio
async def test_fetch_repo_issues_and_prs_zero_limit_short_circuits():
    from app.utils import github_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock()
    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        items = await github_ingest.fetch_repo_issues_and_prs("o", "r", limit=0, min_reactions=1)
    assert items == []
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_repo_issues_and_prs_respects_limit_after_filter():
    from app.utils import github_ingest
    # Over-fetch (limit*2) returns 6, but limit=2 caps post-filter.
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(json_data=[
        {"number": i, "title": f"t{i}", "body": "body",
         "reactions": {"+1": 10, "heart": 0, "hooray": 0},
         "state": "closed", "closed_at": "", "html_url": ""}
        for i in range(6)
    ]))
    with patch("app.utils.github_ingest.get_github_client", return_value=mock_client):
        items = await github_ingest.fetch_repo_issues_and_prs(
            "o", "r", limit=2, min_reactions=1,
        )
    assert len(items) == 2
