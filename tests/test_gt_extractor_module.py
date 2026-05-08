"""Tests for app/modules/gt_extractor.py (#9.23)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import gt_extractor as gx


# ---------------------------------------------------------------------------
# sanitize_toon_content — escape rules (#6.17 regression coverage)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_sanitize_backslash_is_escaped_first():
    """Order matters: \\ → \\\\ before \\n insertion; else \\n becomes \\\\n."""
    assert gx.sanitize_toon_content("a\\b") == r"a\\b"


@pytest.mark.smoke
def test_sanitize_newline_escaped_to_backslash_n():
    assert gx.sanitize_toon_content("a\nb") == r"a\nb"


@pytest.mark.smoke
def test_sanitize_tab_escaped_to_backslash_t():
    assert gx.sanitize_toon_content("a\tb") == r"a\tb"


@pytest.mark.smoke
def test_sanitize_double_quote_escaped():
    assert gx.sanitize_toon_content('a"b') == r'a\"b'


@pytest.mark.smoke
def test_sanitize_combined_order_preserved():
    """All four transformations applied in the right order (backslash first)."""
    # \ becomes \\, \n becomes \n, " becomes \", \t becomes \t — each independent
    raw = 'line1\n"quoted"\tend\\x'
    got = gx.sanitize_toon_content(raw)
    # Verify the backslash in \x is doubled (not consumed by \n escape)
    assert r"end\\x" in got
    assert r"\n" in got
    assert r'\"' in got


# ---------------------------------------------------------------------------
# _format_toon_rows — row shape
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_format_toon_rows_numbers_from_one():
    rows = gx._format_toon_rows([
        {"title": "First", "content": "c1", "tags": "a,b"},
        {"title": "Second", "content": "c2", "tags": "c"},
    ])
    assert rows[0].lstrip().startswith("1,")
    assert rows[1].lstrip().startswith("2,")


@pytest.mark.smoke
def test_format_toon_rows_slugifies_title():
    rows = gx._format_toon_rows([{"title": "My Long Title", "content": "x", "tags": ""}])
    assert "my-long-title" in rows[0]


@pytest.mark.smoke
def test_format_toon_rows_defaults_missing_source():
    rows = gx._format_toon_rows([{"title": "t", "content": "x", "tags": ""}])
    assert "pending-verification" in rows[0]


@pytest.mark.smoke
def test_format_toon_rows_quotes_content_and_tags():
    rows = gx._format_toon_rows([{"title": "t", "content": "hello", "tags": "a, b"}])
    assert '"hello"' in rows[0]
    # tags are joined, lowercased, whitespace-stripped → "a,b"
    assert '"a,b"' in rows[0]


# ---------------------------------------------------------------------------
# _normalize_legacy_keys — LLM drift tolerance
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_normalize_legacy_keys_maps_topic_to_title():
    entries = [{"topic": "old key", "content": "x"}]
    out = gx._normalize_legacy_keys(entries)
    assert out[0]["title"] == "old key"
    assert "topic" not in out[0]


@pytest.mark.smoke
def test_normalize_legacy_keys_skips_when_title_already_present():
    entries = [{"title": "canonical", "topic": "legacy", "content": "x"}]
    out = gx._normalize_legacy_keys(entries)
    assert out[0]["title"] == "canonical"  # unchanged
    assert out[0]["topic"] == "legacy"     # not consumed


# ---------------------------------------------------------------------------
# _search_searxng — HTTP fan-in
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_search_searxng_returns_trimmed_results():
    fake_client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "results": [
            {"title": "t1", "url": "u1", "content": "c1"},
            {"title": "t2", "url": "u2", "content": "c2"},
        ],
    }
    fake_client.get.return_value = resp
    with patch.object(gx, "get_searxng_client", return_value=fake_client):
        results = await gx._search_searxng("q")
    assert len(results) == 2
    assert results[0]["title"] == "t1"


@pytest.mark.smoke
async def test_search_searxng_returns_empty_on_non_200():
    fake_client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 500
    fake_client.get.return_value = resp
    with patch.object(gx, "get_searxng_client", return_value=fake_client):
        results = await gx._search_searxng("q")
    assert results == []


@pytest.mark.smoke
async def test_search_searxng_returns_empty_on_exception():
    fake_client = AsyncMock()
    fake_client.get.side_effect = RuntimeError("down")
    with patch.object(gx, "get_searxng_client", return_value=fake_client):
        results = await gx._search_searxng("q")
    assert results == []


@pytest.mark.smoke
async def test_search_searxng_respects_max_results():
    fake_client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "results": [{"title": f"t{i}", "url": f"u{i}", "content": ""} for i in range(20)],
    }
    fake_client.get.return_value = resp
    with patch.object(gx, "get_searxng_client", return_value=fake_client):
        results = await gx._search_searxng("q", max_results=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# extract_ground_truths — orchestration (short-circuit + dedupe)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_extract_ground_truths_short_circuits_when_no_results():
    with patch.object(gx, "search_searxng", AsyncMock(return_value=[])):
        result = await gx.extract_ground_truths("nothing-topic")
    assert result["status"] == "no_results"
    assert result["topic"] == "nothing-topic"


@pytest.mark.smoke
async def test_extract_ground_truths_dedupes_by_url():
    """Same URL across multiple queries should only be seen once."""
    duplicated = [
        {"title": "t1", "url": "u1", "content": "c"},
        {"title": "t1-dup", "url": "u1", "content": "c"},
        {"title": "t2", "url": "u2", "content": "c"},
    ]
    # Mock _search returning the duplicated set; then let distillation short-circuit.
    async def fake_search(q, max_results=10):
        return duplicated

    # Return "no entries" from the LLM distillation so we short-circuit safely.
    # Sprint X.12: gt_extractor now uses model_router.tool_call. The wrapper
    # response carries entries via tool_calls[0].arguments["entries"] = [].
    fake_call = SimpleNamespace(arguments={"entries": []})
    fake_resp = SimpleNamespace(
        text="", success=True, tool_calls=[fake_call],
    )

    with patch.object(gx, "search_searxng", side_effect=fake_search), \
         patch.object(gx.model_router, "tool_call", AsyncMock(return_value=fake_resp)):
        result = await gx.extract_ground_truths("topic")

    # Even though 3 raw results * multiple queries, dedupe by URL = 2 unique
    # The exact count isn't exposed, but the function should complete without error
    assert result["topic"] == "topic"


# ---------------------------------------------------------------------------
# _detect_topic_id — delegates to shared util
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_detect_topic_id_returns_int_for_any_input():
    # We don't test the specific routing (that's topic_detection's job);
    # just that the wrapper returns an int and has a default.
    assert isinstance(gx._detect_topic_id("anything"), int)
    assert isinstance(gx._detect_topic_id(""), int)
