"""Tests for the §17.107 HuggingFace deep-mode helpers."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_response(status_code: int = 200, json_data=None, content: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data if json_data is not None else {})
    resp.content = content
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError
        resp.raise_for_status.side_effect = HTTPStatusError("err", request=None, response=resp)
    return resp


@pytest.fixture
def fake_cache_miss():
    """Force fetch_cache to return None (miss) for every get; record puts.

    Used so every test exercises the live-fetch path. To test the cache-hit
    path explicitly, override .get inside the test.
    """
    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.put = AsyncMock(return_value=True)
    with patch("app.utils.hf_ingest.get_fetch_cache", return_value=cache):
        yield cache


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestHfRefParser:
    def test_is_hf_ref_accepts_known_kinds(self):
        from app.modules.research_extractors import _is_hf_ref
        assert _is_hf_ref("hf:model/microsoft/phi-2")
        assert _is_hf_ref("hf:dataset/squad")
        assert _is_hf_ref("hf:dataset/openai/gsm8k")
        assert _is_hf_ref("hf:paper/2310.06825")
        assert _is_hf_ref("hf:space/black-forest-labs/FLUX.1-schnell")

    def test_is_hf_ref_rejects_unknown_kind(self):
        from app.modules.research_extractors import _is_hf_ref
        assert not _is_hf_ref("hf:user/foo")
        assert not _is_hf_ref("hf:doc/transformers")  # deferred
        assert not _is_hf_ref("https://hf.co/x")
        assert not _is_hf_ref("hf:")
        assert not _is_hf_ref("hf:model")  # missing id

    def test_parse_hf_ref_model(self):
        from app.modules.research_extractors import _parse_hf_ref
        assert _parse_hf_ref("hf:model/microsoft/phi-2") == ("model", "microsoft/phi-2")

    def test_parse_hf_ref_dataset(self):
        from app.modules.research_extractors import _parse_hf_ref
        assert _parse_hf_ref("hf:dataset/squad") == ("dataset", "squad")
        assert _parse_hf_ref("hf:dataset/openai/gsm8k") == ("dataset", "openai/gsm8k")

    def test_parse_hf_ref_paper(self):
        from app.modules.research_extractors import _parse_hf_ref
        assert _parse_hf_ref("hf:paper/2310.06825") == ("paper", "2310.06825")

    def test_parse_hf_ref_space(self):
        from app.modules.research_extractors import _parse_hf_ref
        assert _parse_hf_ref("hf:space/owner/demo") == ("space", "owner/demo")

    def test_parse_hf_ref_rejects_bad_id(self):
        from app.modules.research_extractors import _parse_hf_ref
        for bad in ["hf:model/has space", "hf:model/evil;rm", "hf:model/" + "x" * 129]:
            with pytest.raises(ValueError):
                _parse_hf_ref(bad)


# ---------------------------------------------------------------------------
# fetch_hf_model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hf_model_returns_readme_and_metadata(fake_cache_miss):
    from app.utils import hf_ingest

    # /api/models/{id} returns metadata with sha + cardData
    meta = {
        "sha": "abc123def456",
        "pipeline_tag": "text-generation",
        "library_name": "transformers",
        "downloads": 12345,
        "likes": 678,
        "tags": ["text-generation", "llama"],
        "cardData": {
            "license": "apache-2.0",
            "base_model": "meta-llama/Llama-3-8B",
            "model-index": [{
                "results": [{
                    "task": {"type": "text-generation"},
                    "dataset": {"name": "MMLU"},
                    "metrics": [{"type": "accuracy", "value": 0.65}],
                }],
            }],
        },
    }
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=[
        _make_response(json_data=meta),                                  # /api/models/{id}
        _make_response(content=b"# Model Card\nIt does things.\n"),      # README raw
    ])
    with patch("app.utils.hf_ingest.get_huggingface_client", return_value=mock_client):
        out = await hf_ingest.fetch_hf_model("owner/repo")

    by_path = {e["path"]: e for e in out}
    assert "hf:model/owner/repo/README.md" in by_path
    assert "hf:model/owner/repo/metadata" in by_path

    readme_entry = by_path["hf:model/owner/repo/README.md"]
    assert readme_entry["source_type"] == "model_card"
    assert readme_entry["source_ref"] == "abc123def456"
    assert readme_entry["quality_signal"]["downloads"] == 12345
    assert readme_entry["quality_signal"]["likes"] == 678

    meta_entry = by_path["hf:model/owner/repo/metadata"]
    assert "pipeline_tag: text-generation" in meta_entry["content"]
    assert "library_name: transformers" in meta_entry["content"]
    assert "license: apache-2.0" in meta_entry["content"]
    assert "text-generation on MMLU: accuracy=0.65" in meta_entry["content"]


@pytest.mark.asyncio
async def test_fetch_hf_model_skips_when_readme_404(fake_cache_miss):
    from app.utils import hf_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=[
        _make_response(json_data={"sha": "abc", "cardData": {"license": "mit"}}),
        _make_response(status_code=404),  # no README
    ])
    with patch("app.utils.hf_ingest.get_huggingface_client", return_value=mock_client):
        out = await hf_ingest.fetch_hf_model("x/y")

    # No README entry, but metadata entry still emitted.
    paths = [e["path"] for e in out]
    assert "hf:model/x/y/README.md" not in paths
    assert "hf:model/x/y/metadata" in paths


@pytest.mark.asyncio
async def test_fetch_hf_model_uses_cache_on_hit():
    from app.utils import hf_ingest

    cached_readme = b"# Cached README\n"
    cache = MagicMock()
    # Cache miss on api call, hit on README
    async def fake_get(stype, ref, path):
        if path.endswith("/README.md"):
            return cached_readme
        return None
    cache.get = AsyncMock(side_effect=fake_get)
    cache.put = AsyncMock(return_value=True)

    meta = {"sha": "shaCACHED", "cardData": {}}
    mock_client = MagicMock()
    # Note: when README hits cache, no HTTP call for the README is made,
    # so only the /api/models/{id} call should hit the client.
    mock_client.get = AsyncMock(return_value=_make_response(json_data=meta))

    with patch("app.utils.hf_ingest.get_fetch_cache", return_value=cache), \
         patch("app.utils.hf_ingest.get_huggingface_client", return_value=mock_client):
        out = await hf_ingest.fetch_hf_model("owner/cached")

    # Exactly one HTTP call (the API metadata), README came from cache.
    assert mock_client.get.call_count == 1
    readmes = [e for e in out if e["path"].endswith("/README.md")]
    assert len(readmes) == 1
    assert readmes[0]["content"] == "# Cached README\n"


@pytest.mark.asyncio
async def test_fetch_hf_model_zero_budget_returns_empty(fake_cache_miss):
    from app.utils import hf_ingest
    from app.config import settings
    with patch.object(settings, "hf_max_files", 0):
        out = await hf_ingest.fetch_hf_model("any/repo")
    assert out == []


# ---------------------------------------------------------------------------
# fetch_hf_dataset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hf_dataset_returns_card_and_summary(fake_cache_miss):
    from app.utils import hf_ingest
    meta = {
        "sha": "ds-sha-1",
        "downloads": 999,
        "likes": 10,
        "tags": ["language:en"],
        "cardData": {
            "license": "cc-by-4.0",
            "language": ["en"],
            "task_categories": ["question-answering"],
            "size_categories": ["10K<n<100K"],
        },
    }
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=[
        _make_response(json_data=meta),
        _make_response(content=b"# Dataset Card\nA QA dataset.\n"),
    ])
    with patch("app.utils.hf_ingest.get_huggingface_client", return_value=mock_client):
        out = await hf_ingest.fetch_hf_dataset("squad")

    by_path = {e["path"]: e for e in out}
    assert "hf:dataset/squad/README.md" in by_path
    assert "hf:dataset/squad/metadata" in by_path
    assert by_path["hf:dataset/squad/README.md"]["source_type"] == "dataset_card"
    assert "task_categories" in by_path["hf:dataset/squad/metadata"]["content"]


# ---------------------------------------------------------------------------
# fetch_hf_paper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hf_paper_renders_abstract_and_linked_models(fake_cache_miss):
    from app.utils import hf_ingest
    meta = {
        "title": "Test Paper Title",
        "summary": "This paper proposes a thing.",
        "authors": [{"name": "Alice"}, {"name": "Bob"}],
        "models": [{"id": "alice/model-a"}, {"id": "alice/model-b"}],
        "datasets": [{"id": "alice/ds"}],
        "upvotes": 42,
        "publishedAt": "2025-01-01T00:00:00Z",
    }
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(json_data=meta))

    with patch("app.utils.hf_ingest.get_huggingface_client", return_value=mock_client):
        out = await hf_ingest.fetch_hf_paper("2310.06825")

    assert len(out) == 1
    e = out[0]
    assert e["source_type"] == "paper_abstract"
    assert e["source_ref"] == "2310.06825"
    assert "Test Paper Title" in e["content"]
    assert "Alice, Bob" in e["content"]
    assert "model: alice/model-a" in e["content"]
    assert "dataset: alice/ds" in e["content"]
    assert e["quality_signal"]["linked_models"] == 2
    assert e["quality_signal"]["upvotes"] == 42


@pytest.mark.asyncio
async def test_fetch_hf_paper_empty_summary_returns_empty(fake_cache_miss):
    from app.utils import hf_ingest
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_response(
        json_data={"title": "T", "summary": ""},
    ))
    with patch("app.utils.hf_ingest.get_huggingface_client", return_value=mock_client):
        out = await hf_ingest.fetch_hf_paper("0000.0000")
    assert out == []


# ---------------------------------------------------------------------------
# fetch_hf_space
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hf_space_returns_readme_and_metadata(fake_cache_miss):
    from app.utils import hf_ingest
    meta = {
        "sha": "space-sha",
        "sdk": "gradio",
        "likes": 5,
        "runtime": {"stage": "RUNNING"},
        "cardData": {"title": "Cool Demo", "emoji": "🚀", "license": "mit"},
    }
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=[
        _make_response(json_data=meta),
        _make_response(content=b"# Space README\nThis demos foo.\n"),
    ])
    with patch("app.utils.hf_ingest.get_huggingface_client", return_value=mock_client):
        out = await hf_ingest.fetch_hf_space("owner/demo")

    by_path = {e["path"]: e for e in out}
    assert by_path["hf:space/owner/demo/README.md"]["source_type"] == "tech_docs"
    assert "sdk: gradio" in by_path["hf:space/owner/demo/metadata"]["content"]
    assert "runtime: RUNNING" in by_path["hf:space/owner/demo/metadata"]["content"]


# ---------------------------------------------------------------------------
# fetch_hf dispatch helper + error mapping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_hf_dispatches_by_kind(fake_cache_miss):
    from app.utils import hf_ingest
    with patch("app.utils.hf_ingest.fetch_hf_model", AsyncMock(return_value=[{"x": 1}])) as m, \
         patch("app.utils.hf_ingest.fetch_hf_dataset", AsyncMock(return_value=[{"x": 2}])) as d, \
         patch("app.utils.hf_ingest.fetch_hf_paper", AsyncMock(return_value=[{"x": 3}])) as p, \
         patch("app.utils.hf_ingest.fetch_hf_space", AsyncMock(return_value=[{"x": 4}])) as s:
        assert await hf_ingest.fetch_hf("model", "a/b") == [{"x": 1}]
        assert await hf_ingest.fetch_hf("dataset", "a/b") == [{"x": 2}]
        assert await hf_ingest.fetch_hf("paper", "2310.0") == [{"x": 3}]
        assert await hf_ingest.fetch_hf("space", "a/b") == [{"x": 4}]
        m.assert_awaited_once()
        d.assert_awaited_once()
        p.assert_awaited_once()
        s.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_hf_unknown_kind_raises():
    from app.utils import hf_ingest
    with pytest.raises(ValueError, match="Unknown HF kind"):
        await hf_ingest.fetch_hf("nope", "anything")


@pytest.mark.asyncio
async def test_check_response_maps_404_to_not_found():
    from app.utils import hf_ingest
    with pytest.raises(hf_ingest.HFNotFoundError):
        hf_ingest._check_response(_make_response(status_code=404), "ctx")


@pytest.mark.asyncio
async def test_check_response_maps_429_to_rate_limit():
    from app.utils import hf_ingest
    with pytest.raises(hf_ingest.HFRateLimitError):
        hf_ingest._check_response(_make_response(status_code=429), "ctx")
