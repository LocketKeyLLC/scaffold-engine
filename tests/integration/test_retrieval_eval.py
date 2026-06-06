"""§17.430 — live retrieval-quality regression gate.

Upgrades the binary substring goldens (test_retrieval_golden.py: "expected
title in top-3, pass/fail") into a GRADED gate: it runs the curated golden
queries through the real query_rag pipeline (hybrid retrieval + CrossEncoder
rerank) and scores ranking quality with the deterministic metrics in
app.utils.retrieval_metrics (hit@k / MRR / nDCG@k — no LLM judge). The
aggregate metrics are asserted against a recorded baseline floor so a
retrieval regression (e.g. an embedder/reranker/fusion change that reorders
results) fails the gate instead of passing silently.

Relevance = the result title contains any labeled substring (re-ingestion-
robust; see §17.211 / §17.230). Uses the same curated queries as
test_retrieval_golden (known present in the corpus), so the gate measures
RANKING QUALITY rather than corpus coverage gaps.

Placement: tests/integration/ → excluded from the "no live services" CI job
(`pytest -k "not integration"`) and from tier-1 ci-smoke (collect_ignore in
tests/conftest.py). The dev `make test` runs it but skips when Milvus is
empty. @timeout(900) per the integration-live-test convention.
"""
from __future__ import annotations

from statistics import mean

import pytest

from app.modules.rag_pipeline import query_rag
from app.utils.retrieval_metrics import hit_at_k, ndcg_at_k, reciprocal_rank
from tests._milvus_helpers import skip_if_milvus_empty

pytestmark = pytest.mark.asyncio

# (query, domain, [relevant title substrings — OR-matched, case-insensitive])
EVAL_GOLDENS = [
    ("How does function calling work in LLM tool use?", "prompt", ["function-calling", "function calling"]),
    ("What is chain of thought prompting?", "prompt", ["prompt engineering", "chain-of-thought", "chain of thought"]),
    ("How does hybrid search combine dense and sparse retrieval?", "rag", ["hybrid"]),
    ("What is quantization and how does it reduce model size?", "llm", ["quantiz"]),
    ("Describe the TOON file format specification and its pipeline stages", "spec", ["toon"]),
    ("What are common software design patterns like singleton or factory?", "eng", ["pattern"]),
    ("Explain the principles of test-driven development", "eng", ["test"]),
]

_TOP_K = 10

# §17.430 baseline floors. Measured live baseline (OVERVIEW §17.430, KB
# ~1011 entries): mean hit@5 = nDCG@10 = MRR = 1.000 (every curated query's
# relevant doc ranks #1). Floors sit below that with margin: a single
# borderline query degrading still passes (corpus flux tolerance), but 2+
# queries regressing trips the gate. e.g. 2 of 7 missing top-5 → hit@5 0.714
# < 0.80. Tighten as the corpus/pipeline stabilize; loosen only with reason.
_FLOOR_MEAN_HIT_AT_5 = 0.80
_FLOOR_MEAN_NDCG_AT_10 = 0.75
_FLOOR_MRR = 0.75


def _relevance(titles: list[str], substrs: list[str]) -> list[bool]:
    lowered = [t.lower() for t in titles]
    subs = [s.lower() for s in substrs]
    return [any(s in t for s in subs) for t in lowered]


@pytest.mark.timeout(900)
async def test_retrieval_eval_gate():
    skip_if_milvus_empty()

    hits5: list[float] = []
    ndcgs: list[float] = []
    rrs: list[float] = []
    rows: list[str] = []

    for query, domain, substrs in EVAL_GOLDENS:
        result = await query_rag(query, domain=domain, top_k=_TOP_K)
        titles = [r["title"] for r in result["results"]]
        rels = _relevance(titles, substrs)

        h5 = hit_at_k(rels, 5)
        nd = ndcg_at_k(rels, _TOP_K)
        rr = reciprocal_rank(rels, _TOP_K)
        hits5.append(h5)
        ndcgs.append(nd)
        rrs.append(rr)
        rows.append(
            f"  {domain:10s} hit@5={h5:.0f} ndcg@10={nd:.3f} rr={rr:.3f}  {query[:48]!r}"
        )

    m_hit5, m_ndcg, m_mrr = mean(hits5), mean(ndcgs), mean(rrs)
    report = (
        f"\n§17.430 retrieval eval (n={len(EVAL_GOLDENS)}, top_k={_TOP_K}):\n"
        + "\n".join(rows)
        + f"\n  MEAN hit@5={m_hit5:.3f} nDCG@10={m_ndcg:.3f} MRR={m_mrr:.3f}\n"
    )
    print(report)

    assert m_hit5 >= _FLOOR_MEAN_HIT_AT_5, f"mean hit@5 {m_hit5:.3f} < floor {_FLOOR_MEAN_HIT_AT_5}{report}"
    assert m_ndcg >= _FLOOR_MEAN_NDCG_AT_10, f"mean nDCG@10 {m_ndcg:.3f} < floor {_FLOOR_MEAN_NDCG_AT_10}{report}"
    assert m_mrr >= _FLOOR_MRR, f"MRR {m_mrr:.3f} < floor {_FLOOR_MRR}{report}"
