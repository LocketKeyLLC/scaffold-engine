"""§17.1110 (Phase 1 ledger L-1b) — rerank once over the fused union, cap the
per-query shortlist.

§17.1109 measured the CPU CrossEncoder at 16–34 s per 10-doc query in the
assist research path, paid four times per fix turn. These pin the mechanism:
the guide pre-pass retrieves the KB once for all queries (per-query RRF
shortlists of 5, fused by summed RRF, ONE rerank against the step text) and
the fix need-query reranks 5 candidates, not 10.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.modules import assist_research_lib as lib
from app.modules import rag_pipeline as rp


def _doc(eid: str, rrf: float, title: str = "t") -> dict:
    return {"entry_id": eid, "content": f"content {eid}", "title": title, "tags": "",
            "source_url": "", "domain": "eng", "version": 1, "supersedes_id": "",
            "confidence_score": 0.0, "source_type": "", "scores": {"rrf": rrf, "vector": 0.5, "keyword": 0.1}}


def _resp(*docs: dict) -> dict:
    return {"status": "ok", "results": list(docs), "metadata": {}}


# ── settings ─────────────────────────────────────────────────────────────────

def test_assist_rerank_defaults_are_the_measured_trade():
    """§17.1110 set 5/8 when a pair cost 1.4 s; §17.1125 measured 10/16 on the
    real guide-query corpus once a pair cost 54 ms (§17.1124)."""
    assert settings.assist_research_rerank_once is True
    assert settings.assist_rerank_max_candidates == 10
    assert settings.assist_rerank_union_candidates == 16
    assert settings.rerank_max_candidates == 10, "the global /rag + execution cap is untouched"


# ── query_rag_multi ──────────────────────────────────────────────────────────

async def test_multi_fuses_by_summed_rrf_and_reranks_exactly_once():
    calls: list[tuple] = []

    async def fake_query_rag(q, **kw):
        calls.append((q, kw))
        return {"q1": _resp(_doc("A", 0.5), _doc("B", 0.3)),
                "q2": _resp(_doc("B", 0.4), _doc("C", 0.2)),
                "q3": _resp(_doc("D", 0.1))}[q]

    async def fake_rerank(query, results, top_k, *, max_candidates=None, doc_truncate=None):
        # score = reversed order so the test can see the reranker's say
        ranked = [rp.replace(r, rerank_score=1.0 - i * 0.05, final_score=1.0 - i * 0.05)
                  for i, r in enumerate(results)]
        fake_rerank.seen = (query, [r.entry_id for r in results], top_k, max_candidates)
        return ranked, {"backend": "CROSS_ENCODER", "skipped_rerank": False, "warnings": []}

    with patch.object(rp, "query_rag", fake_query_rag), patch.object(rp, "_rerank", fake_rerank):
        out = await rp.query_rag_multi(["q1", "q2", "q3"], rerank_query="Configure the reverse proxy",
                                       top_k=5, per_query_candidates=5, union_candidates=8)

    # per-query: embed + Milvus only, shortlist of 5
    assert [c[0] for c in calls] == ["q1", "q2", "q3"]
    assert all(c[1]["skip_rerank"] is True and c[1]["top_k"] == 5 for c in calls)
    # union of 4 docs; B hit by two queries → summed RRF 0.7 leads
    q, ids, top_k, max_cand = fake_rerank.seen
    assert q == "Configure the reverse proxy"
    assert ids == ["B", "A", "C", "D"] and max_cand == 8 and top_k == 5
    md = out["metadata"]
    assert md["queries"] == 3 and md["union_size"] == 4 and md["rerank_candidates"] == 4
    assert md["reranked"] is True
    by = {d["entry_id"]: d for d in out["results"]}
    assert by["B"]["scores"]["rrf"] == 0.7 and by["B"]["scores"]["query_hits"] == 2
    assert by["A"]["scores"]["query_hits"] == 1


async def test_multi_caps_the_union_before_the_single_rerank():
    async def fake_query_rag(q, **kw):
        return _resp(*[_doc(f"{q}-{i}", 0.9 - i * 0.1) for i in range(5)])

    async def fake_rerank(query, results, top_k, *, max_candidates=None, doc_truncate=None):
        fake_rerank.n = len(results)
        return ([rp.replace(r, final_score=0.95) for r in results],
                {"backend": "CROSS_ENCODER", "skipped_rerank": False, "warnings": []})

    with patch.object(rp, "query_rag", fake_query_rag), patch.object(rp, "_rerank", fake_rerank):
        out = await rp.query_rag_multi(["a", "b", "c"], rerank_query="x", top_k=5,
                                       per_query_candidates=5, union_candidates=8)
    assert fake_rerank.n == 8, "15 unique candidates, only the top 8 by fused RRF are scored"
    assert out["metadata"]["union_size"] == 15 and out["metadata"]["rerank_candidates"] == 8
    assert len(out["results"]) == 5


async def test_multi_confidence_gate_falls_back_to_top3_like_query_rag():
    async def fake_query_rag(q, **kw):
        return _resp(_doc("A", 0.5), _doc("B", 0.4), _doc("C", 0.3), _doc("D", 0.2))

    async def fake_rerank(query, results, top_k, *, max_candidates=None, doc_truncate=None):
        return ([rp.replace(r, final_score=0.1) for r in results],
                {"backend": "CROSS_ENCODER", "skipped_rerank": False, "warnings": []})

    with patch.object(rp, "query_rag", fake_query_rag), patch.object(rp, "_rerank", fake_rerank):
        out = await rp.query_rag_multi(["a"], rerank_query="x", top_k=5, confidence_threshold=0.8)
    assert len(out["results"]) == 3 and out["metadata"]["fell_back_to_top3"] is True
    assert "threshold_relaxed_to_top3" in out["metadata"]["warnings"]


async def test_multi_empty_queries_and_failed_queries_are_fail_soft():
    with patch.object(rp, "query_rag", AsyncMock()) as qr, patch.object(rp, "_rerank", AsyncMock()) as rr:
        out = await rp.query_rag_multi(["", "  "], rerank_query="x")
    assert out["results"] == [] and qr.await_count == 0 and rr.await_count == 0

    async def boom(q, **kw):
        raise RuntimeError("milvus down")

    with patch.object(rp, "query_rag", boom), patch.object(rp, "_rerank", AsyncMock()) as rr:
        out = await rp.query_rag_multi(["a", "b"], rerank_query="x")
    assert out["results"] == [] and "query_failed" in out["metadata"]["warnings"]
    assert rr.await_count == 0, "nothing to rerank"


# ── the assist paths ─────────────────────────────────────────────────────────

async def test_prepass_retrieves_the_kb_once_and_the_web_per_query(monkeypatch):
    monkeypatch.setattr(lib.settings, "assist_research_rerank_once", True)
    seen: dict = {"confirm": []}

    async def fake_detect(**kw):
        return ["q1", "q2", "q3"]

    async def fake_union(queries, *, rerank_query, node_key, domain):
        seen["union"] = (list(queries), rerank_query, node_key, domain)
        return "[1] KB hit\n    some content"

    async def fake_confirm(q, *, node_key, domain, deep=False, include_kb=True, **kw):
        seen["confirm"].append((q, include_kb))
        return [{"query": q, "kind": "searxng", "text": f"web {q}"}]

    with patch.object(lib, "_detect_unknowns", fake_detect), \
         patch.object(lib, "_kb_union_block", fake_union), \
         patch.object(lib, "_confirm_query", fake_confirm):
        sources = await lib._research_prepass(task_text="Configure the reverse proxy", tool="Shell",
                                              role="model_general", max_queries=3, node_key="N1",
                                              domain="eng", deep=True)

    assert seen["union"] == (["q1", "q2", "q3"], "Configure the reverse proxy", "N1", "eng")
    assert seen["confirm"] == [("q1", False), ("q2", False), ("q3", False)]
    kinds = [s["kind"] for s in sources]
    assert kinds.count("milvus") == 1 and kinds.count("searxng") == 3
    assert sources[0] == {"query": "q1 | q2 | q3", "kind": "milvus", "text": "[1] KB hit\n    some content"}


async def test_prepass_valve_off_keeps_the_per_query_path(monkeypatch):
    monkeypatch.setattr(lib.settings, "assist_research_rerank_once", False)
    seen: list = []

    async def fake_detect(**kw):
        return ["q1", "q2"]

    async def fake_confirm(q, *, node_key, domain, deep=False, **kw):
        seen.append((q, kw.get("include_kb", True)))
        return []

    with patch.object(lib, "_detect_unknowns", fake_detect), \
         patch.object(lib, "_kb_union_block", AsyncMock()) as union, \
         patch.object(lib, "_confirm_query", fake_confirm):
        await lib._research_prepass(task_text="t", tool="Shell", role="r", max_queries=3,
                                    node_key="N1", domain=None)
    assert union.await_count == 0 and seen == [("q1", True), ("q2", True)]


async def test_prepass_single_query_does_not_union(monkeypatch):
    monkeypatch.setattr(lib.settings, "assist_research_rerank_once", True)

    async def fake_detect(**kw):
        return ["only"]

    async def fake_confirm(q, *, node_key, domain, deep=False, **kw):
        assert kw.get("include_kb", True) is True
        return []

    with patch.object(lib, "_detect_unknowns", fake_detect), \
         patch.object(lib, "_kb_union_block", AsyncMock()) as union, \
         patch.object(lib, "_confirm_query", fake_confirm):
        await lib._research_prepass(task_text="t", tool="Shell", role="r", max_queries=3,
                                    node_key="N1", domain=None)
    assert union.await_count == 0


async def test_confirm_query_kb_half_uses_the_assist_cap_and_can_be_skipped():
    from app.modules import execution_agent as ea
    ms = AsyncMock(return_value="[1] x\n    y")
    sx = AsyncMock(return_value="")
    with patch.object(ea, "_milvus_search", ms), patch.object(ea, "_searxng_search", sx):
        out = await lib._confirm_query("q", node_key="N1", domain="eng", deep=False)
        assert ms.await_args.kwargs["max_candidates"] == settings.assist_rerank_max_candidates == 10
        assert any(s["kind"] == "milvus" for s in out)
        ms.reset_mock()
        out = await lib._confirm_query("q", node_key="N1", domain="eng", deep=False, include_kb=False)
        assert ms.await_count == 0 and not any(s["kind"] == "milvus" for s in out)


async def test_milvus_search_passes_the_cap_through_and_defaults_to_none():
    from app.modules import execution_agent as ea
    qr = AsyncMock(return_value={"results": [], "metadata": {}})
    with patch.object(ea, "query_rag", qr):
        await ea._milvus_search("q", node_key="N1", domain=None, max_candidates=5)
        assert qr.await_args.kwargs["max_candidates"] == 5
        await ea._milvus_search("q")
        assert qr.await_args.kwargs["max_candidates"] is None, "execution nodes keep the global cap"


async def test_kb_union_block_renders_like_milvus_search():
    resp = {"results": [_doc("A", 0.5, title="Title A") | {"scores": {"final": 0.9}}],
            "metadata": {"queries": 3, "union_size": 4, "rerank_candidates": 4, "reranked": True, "latency_ms": 12.0}}
    with patch.object(rp, "query_rag_multi", AsyncMock(return_value=resp)):
        text = await lib._kb_union_block(["a", "b"], rerank_query="x", node_key="N1", domain=None)
    assert text.startswith("[1] Title A\n    content A")
    with patch.object(rp, "query_rag_multi", AsyncMock(side_effect=RuntimeError("down"))):
        assert await lib._kb_union_block(["a"], rerank_query="x", node_key="N1", domain=None) == ""
