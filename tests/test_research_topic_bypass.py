"""Tests for §17.113 — topic-mode classifier-driven distill bypass in _extract_entries.

When _fetch_and_extract returns a body for a URL that classify_url
recognizes as curated, the LLM tool_call must NOT be invoked on that
URL's chunks. Non-curated URLs continue through the existing LLM batch
path. Mixed batches must split correctly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import research_agent as ra


async def _fetched_factory(url_to_body: dict[str, str]):
    """Stub for _fetch_and_extract that returns a list of {url, content}."""
    return [{"url": u, "content": b} for u, b in url_to_body.items()]


@pytest.mark.asyncio
async def test_classified_url_bypasses_llm_extract():
    """All search results classify to a curated source_type → tool_call
    never invoked. Uses two curated source_types — SO answer + HF model
    card — because both are in CURATED_SOURCE_TYPES. Wikipedia is
    intentionally NOT curated (mutable prose; §17.110 design call), so a
    wiki URL would still run through the LLM extract pass.
    """
    results = [
        {"url": "https://stackoverflow.com/a/12345", "title": "SO answer",
         "snippet": "...", "facet": "f1"},
        {"url": "https://huggingface.co/microsoft/phi-2", "title": "HF model",
         "snippet": "...", "facet": "f1"},
    ]
    bodies = {
        results[0]["url"]: "Stack Overflow answer body. " * 30,
        results[1]["url"]: "Model card content. " * 30,
    }
    tool_call_mock = AsyncMock()
    with patch.object(ra, "_fetch_and_extract",
                      AsyncMock(return_value=[{"url": u, "content": b}
                                              for u, b in bodies.items()])), \
         patch.object(ra.model_router, "tool_call", tool_call_mock):
        entries = await ra._extract_entries(results, topic="anything")

    tool_call_mock.assert_not_called()
    assert len(entries) >= 2, f"expected at least 2 entries, got {len(entries)}"
    so_entries = [e for e in entries if e["source"] == results[0]["url"]]
    hf_entries = [e for e in entries if e["source"] == results[1]["url"]]
    assert all(e["source_type"] == "so_answer" for e in so_entries)
    assert all(e["source_type"] == "model_card" for e in hf_entries)
    # Every bypassed entry carries §17.104 provenance.
    for e in so_entries + hf_entries:
        assert "provenance" in e


@pytest.mark.asyncio
async def test_uncurated_url_still_runs_llm():
    """A non-classifying URL → LLM tool_call IS invoked (existing flow)."""
    results = [
        {"url": "https://random-blog.example/post", "title": "blog",
         "snippet": "...", "facet": "f1"},
    ]
    bodies = {results[0]["url"]: "Blog post body. " * 30}
    fake_resp = MagicMock()
    fake_resp.success = True
    fake_resp.text = '{"entries": [{"title": "T", "content": "C", "source": "https://random-blog.example/post"}]}'
    fake_resp.error = None
    # ChatResponse.message.tool_calls path — populate so read_tool_args succeeds
    fake_resp.message = MagicMock()
    fake_tc = MagicMock()
    fake_tc.function.name = "record_entries"
    fake_tc.function.arguments = '{"entries": [{"title": "T", "content": "C", "source": "https://random-blog.example/post"}]}'
    fake_resp.message.tool_calls = [fake_tc]

    tool_call_mock = AsyncMock(return_value=fake_resp)
    with patch.object(ra, "_fetch_and_extract",
                      AsyncMock(return_value=[{"url": u, "content": b}
                                              for u, b in bodies.items()])), \
         patch.object(ra.model_router, "tool_call", tool_call_mock):
        entries = await ra._extract_entries(results, topic="anything")

    # LLM was called for this uncurated URL.
    tool_call_mock.assert_called_once()
    assert len(entries) >= 1


@pytest.mark.asyncio
async def test_mixed_curated_and_uncurated_splits_correctly():
    """Curated URL → bypass. Uncurated URL → LLM. Both produce entries."""
    results = [
        {"url": "https://stackoverflow.com/a/12345", "title": "SO",
         "snippet": "...", "facet": "f1"},
        {"url": "https://random-blog.example/post", "title": "blog",
         "snippet": "...", "facet": "f1"},
    ]
    bodies = {
        results[0]["url"]: "SO body. " * 30,
        results[1]["url"]: "Blog body. " * 30,
    }

    fake_resp = MagicMock()
    fake_resp.success = True
    fake_resp.text = '{"entries": [{"title": "T", "content": "C", "source": "https://random-blog.example/post"}]}'
    fake_resp.error = None
    fake_resp.message = MagicMock()
    fake_tc = MagicMock()
    fake_tc.function.name = "record_entries"
    fake_tc.function.arguments = '{"entries": [{"title": "T", "content": "C", "source": "https://random-blog.example/post"}]}'
    fake_resp.message.tool_calls = [fake_tc]

    tool_call_mock = AsyncMock(return_value=fake_resp)
    with patch.object(ra, "_fetch_and_extract",
                      AsyncMock(return_value=[{"url": u, "content": b}
                                              for u, b in bodies.items()])), \
         patch.object(ra.model_router, "tool_call", tool_call_mock):
        entries = await ra._extract_entries(results, topic="anything")

    # LLM called exactly once — on the uncurated URL batch.
    tool_call_mock.assert_called_once()
    # tool_call's user prompt should contain the blog URL but NOT the SO URL.
    sent_user_msg = tool_call_mock.call_args.kwargs["messages"][1]["content"]
    assert "random-blog.example" in sent_user_msg
    assert "stackoverflow.com" not in sent_user_msg

    # Both URL types produced entries.
    sources = {e["source"] for e in entries}
    assert "https://stackoverflow.com/a/12345" in sources
    assert "https://random-blog.example/post" in sources


@pytest.mark.asyncio
async def test_classified_url_without_body_falls_through():
    """If a classified URL has NO fetched body, no bypass — falls through
    to the existing snippet-fallback path (which doesn't have provenance)."""
    results = [
        {"url": "https://stackoverflow.com/a/12345", "title": "SO no-body",
         "snippet": "snippet text", "content": "snippet text", "facet": "f1"},
    ]
    # _fetch_and_extract returns nothing for this URL.
    fake_resp = MagicMock()
    fake_resp.success = True
    fake_resp.text = '{"entries": []}'
    fake_resp.error = None
    fake_resp.message = MagicMock()
    fake_resp.message.tool_calls = []

    tool_call_mock = AsyncMock(return_value=fake_resp)
    with patch.object(ra, "_fetch_and_extract", AsyncMock(return_value=[])), \
         patch.object(ra.model_router, "tool_call", tool_call_mock):
        entries = await ra._extract_entries(results, topic="anything")

    # No body → no bypass possible. LLM was called (and returned no entries,
    # so the snippet fallback path produced one entry via _score_source).
    tool_call_mock.assert_called_once()
    # The fallback entry is tagged source_type=community (legacy default),
    # NOT so_answer (bypass requires a fetched body).
    if entries:
        assert all(e["source_type"] == "community" for e in entries), \
            f"unexpected source_types: {[e['source_type'] for e in entries]}"
        assert not any("provenance" in e for e in entries), \
            "fallback path should not synthesize provenance"
