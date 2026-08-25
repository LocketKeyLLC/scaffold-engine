"""§17.798 — citation-faithfulness (per-citation attribution) scoring.

Two layers: the deterministic ``parse_citations`` parser (pure, the CI-gated
part) and ``score_citation_faithfulness`` (LLM judge mocked, fail-soft contract).
"""
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest

from app.modules import citation_faithfulness as C


def _resp(results, success=True):
    return SimpleNamespace(
        success=success,
        tool_calls=[SimpleNamespace(arguments={"results": results})],
        text="",
    )


# ───────────────────────── parse_citations (pure) ─────────────────────────

def test_parse_single_marker():
    out = C.parse_citations("Vectors are normalized [2].", n_sources=3)
    assert out == [{"claim": "Vectors are normalized", "source_id": 2, "in_range": True}]


def test_parse_grouped_marker_expands():
    out = C.parse_citations("It fuses dense and sparse [1, 3].", n_sources=3)
    assert [(i["source_id"], i["in_range"]) for i in out] == [(1, True), (3, True)]
    assert all(i["claim"] == "It fuses dense and sparse" for i in out)


def test_parse_adjacent_markers():
    out = C.parse_citations("Both agree [1][2].", n_sources=2)
    assert [i["source_id"] for i in out] == [1, 2]


def test_parse_multi_sentence_attaches_per_sentence():
    text = "Alpha holds [1]. Beta differs [2]."
    out = C.parse_citations(text, n_sources=2)
    assert out == [
        {"claim": "Alpha holds", "source_id": 1, "in_range": True},
        {"claim": "Beta differs", "source_id": 2, "in_range": True},
    ]


def test_parse_out_of_range_flagged_not_dropped():
    out = C.parse_citations("Claim here [9].", n_sources=3)
    assert out == [{"claim": "Claim here", "source_id": 9, "in_range": False}]


def test_parse_ignores_non_numeric_and_markdown_links():
    # [i], [TODO] and a markdown link must NOT be read as citations.
    text = "See [the docs](http://x) and step [i] and [TODO]."
    assert C.parse_citations(text, n_sources=5) == []


def test_parse_dedupes_identical_pairs():
    text = "Same claim [1]. Same claim [1]."
    assert len(C.parse_citations(text, n_sources=1)) == 1


def test_parse_empty_answer():
    assert C.parse_citations("", n_sources=3) == []
    assert C.parse_citations("No citations here at all.", n_sources=3) == []


# ───────────────────── score_citation_faithfulness (judge) ─────────────────────

@pytest.mark.asyncio
async def test_score_basic_ratio():
    answer = "Alpha holds [1]. Beta differs [2]. Gamma is wrong [1]."
    sources = ["source one text", "source two text"]
    # Judge: citation 1 supported, 2 supported, 3 unsupported → 2/3.
    results = [
        {"index": 1, "supported": True},
        {"index": 2, "supported": True},
        {"index": 3, "supported": False},
    ]
    with patch.object(C.model_router, "tool_call", new=AsyncMock(return_value=_resp(results))):
        out = await C.score_citation_faithfulness(answer, sources)
    assert out["total"] == 3 and out["supported"] == 2
    assert out["score"] == 0.67
    assert out["cited"] == 3 and out["dangling"] == 0
    assert out["unsupported_citations"] == [{"claim": "Gamma is wrong", "source_id": 1}]


@pytest.mark.asyncio
async def test_dangling_citation_unsupported_without_llm():
    """An out-of-range [9] is scored unsupported with NO judge call."""
    answer = "Only source [9]."
    sources = ["just one source"]
    mock = AsyncMock(return_value=_resp([]))
    with patch.object(C.model_router, "tool_call", new=mock):
        out = await C.score_citation_faithfulness(answer, sources)
    assert mock.await_count == 0  # no in-range instance → judge never called
    assert out["total"] == 1 and out["supported"] == 0 and out["dangling"] == 1
    assert out["score"] == 0.0
    assert out["unsupported_citations"][0]["dangling"] is True


@pytest.mark.asyncio
async def test_none_on_no_citations():
    """An uncited answer is 'not scored' (attribution undefined)."""
    with patch.object(C.model_router, "tool_call", new=AsyncMock(return_value=_resp([]))):
        out = await C.score_citation_faithfulness("No markers here.", ["s1"])
    assert out is None


@pytest.mark.asyncio
async def test_none_on_empty_input():
    assert await C.score_citation_faithfulness("", ["s1"]) is None
    assert await C.score_citation_faithfulness("Claim [1].", []) is None


@pytest.mark.asyncio
async def test_none_on_llm_failure():
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(return_value=_resp([], success=False))):
        assert await C.score_citation_faithfulness("Claim [1].", ["s1"]) is None


@pytest.mark.asyncio
async def test_none_on_exception_failsoft():
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        assert await C.score_citation_faithfulness("Claim [1].", ["s1"]) is None


@pytest.mark.asyncio
async def test_retries_coax_miss_then_succeeds():
    """§17.560 pattern — a coax miss (success, no parseable results) retries."""
    good = [{"index": 1, "supported": True}]
    mock = AsyncMock(side_effect=[_resp([]), _resp(good)])
    with patch.object(C.model_router, "tool_call", new=mock):
        out = await C.score_citation_faithfulness("Claim here [1].", ["s1"])
    assert out is not None and out["total"] == 1 and out["supported"] == 1
    assert mock.await_count == 2


@pytest.mark.asyncio
async def test_dict_sources_use_text_field():
    answer = "Grounded [1]."
    sources = [{"text": "the backing source", "url": "http://x"}]
    with patch.object(C.model_router, "tool_call",
                      new=AsyncMock(return_value=_resp([{"index": 1, "supported": True}]))):
        out = await C.score_citation_faithfulness(answer, sources)
    assert out["score"] == 1.0 and out["supported"] == 1


@pytest.mark.asyncio
async def test_missing_verdict_counts_unverified_after_rejudge_fails():
    """§17.833 (audit M8) — a citation the judge omits, even after the
    re-judge of the omitted subset, is `unverified` and stays OUT of the
    score's denominator (pre-§17.833 it silently counted as unsupported,
    deflating live scores)."""
    answer = "One [1]. Two [1]."  # two distinct claims, same source
    sources = ["s1"]
    first = _resp([{"index": 1, "supported": True}])  # verdict for #2 missing
    empty = _resp([])  # re-judge coax-misses through all attempts
    mock = AsyncMock(side_effect=[first, empty, empty, empty])
    with patch.object(C.model_router, "tool_call", new=mock):
        out = await C.score_citation_faithfulness(answer, sources)
    assert out["total"] == 2 and out["supported"] == 1
    assert out["unverified"] == 1
    assert out["score"] == 1.0  # 1 supported / 1 judged — omission doesn't deflate
    assert out["unsupported_citations"] == []


@pytest.mark.asyncio
async def test_partial_verdict_rejudges_missing_subset():
    """§17.833 — the judge omitting a verdict triggers ONE re-judge of just
    the omitted instances, with batch positions mapped back correctly."""
    answer = "One [1]. Two [1]. Three [1]."
    sources = ["s1"]
    first = _resp([{"index": 1, "supported": True},
                   {"index": 3, "supported": True}])  # omits #2
    second = _resp([{"index": 1, "supported": False}])  # #2 re-judged → False
    mock = AsyncMock(side_effect=[first, second])
    with patch.object(C.model_router, "tool_call", new=mock):
        out = await C.score_citation_faithfulness(answer, sources)
    assert mock.await_count == 2
    assert out["supported"] == 2 and out["unverified"] == 0
    assert out["score"] == 0.67  # 2/3 — the re-judged False lands on claim #2
    assert out["unsupported_citations"] == [{"claim": "Two", "source_id": 1}]


@pytest.mark.asyncio
async def test_all_unverified_returns_none():
    """§17.833 — zero actual rulings (judge answers only bogus indices, twice)
    → None, not a fabricated score."""
    answer = "One [1]."
    sources = ["s1"]
    bogus = _resp([{"index": 99, "supported": True}])
    mock = AsyncMock(return_value=bogus)
    with patch.object(C.model_router, "tool_call", new=mock):
        out = await C.score_citation_faithfulness(answer, sources)
    assert out is None
