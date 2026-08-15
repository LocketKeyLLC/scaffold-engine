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

from app import model_router
from app.modules.faithfulness import score_faithfulness
from app.modules.rag_pipeline import query_rag
from app.utils.retrieval_metrics import (
    context_precision,
    context_recall,
    hit_at_k,
    ndcg_at_k,
    reciprocal_rank,
)
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

# §17.794 — RAGAS floors. context_precision (AP over the label-matched
# retrieved list) and context_recall (fraction of the query's labelled target
# retrieved). Each EVAL_GOLDENS query is single-target (one substring family),
# so context_recall here tracks coverage and context_precision tracks how high
# the relevant doc(s) rank — both 1.000 on the §17.430 baseline. Floors sit
# below with the same corpus-flux margin as the ranking floors above.
_FLOOR_MEAN_CONTEXT_PRECISION = 0.75
_FLOOR_MEAN_CONTEXT_RECALL = 0.80

# §17.794 — faithfulness of an answer GENERATED from the retrieved context
# (RAGAS, LLM judge via app.modules.faithfulness). This adds one generate +
# one judge LLM call per query, so it is scored on a small fixed subset and is
# fail-soft: the floor is asserted only over queries that actually returned a
# score (the coaxed thinking-model judge intermittently misses — §17.560); if
# NONE score, the sub-check is skipped rather than failing the retrieval gate.
_FAITHFULNESS_SUBSET = 3
_FAITHFULNESS_CONTEXT_K = 5
_FAITHFULNESS_CONTEXT_CHARS = 6_000
_FLOOR_MEAN_FAITHFULNESS = 0.60

_ANSWER_SYSTEM = (
    "You are a precise technical assistant. Answer the question using ONLY the "
    "provided context. Be concise (2-4 sentences). If the context does not "
    "cover the question, say so rather than inventing an answer."
)


def _relevance(titles: list[str], substrs: list[str]) -> list[bool]:
    lowered = [t.lower() for t in titles]
    subs = [s.lower() for s in substrs]
    return [any(s in t for s in subs) for t in lowered]


def _build_context(rows: list[dict], k: int = _FAITHFULNESS_CONTEXT_K) -> str:
    chunks = []
    for r in rows[:k]:
        content = (r.get("content") or "").strip()
        if content:
            title = (r.get("title") or "").strip()
            chunks.append(f"[{title}]\n{content}" if title else content)
    return "\n\n".join(chunks)[:_FAITHFULNESS_CONTEXT_CHARS]


async def _faithfulness_for(query: str, rows: list[dict]) -> float | None:
    """Generate an answer from the retrieved context, RAGAS-score it. Fail-soft."""
    context = _build_context(rows)
    if not context:
        return None
    resp = await model_router.generate(
        prompt=f"Question: {query}\n\nContext:\n{context}",
        role="model_general",
        system=_ANSWER_SYSTEM,
        temperature=0.0,
        max_tokens=512,
    )
    answer = (getattr(resp, "text", "") or "").strip()
    if not answer:
        return None
    scored = await score_faithfulness(answer, context)
    return scored["score"] if scored else None


@pytest.mark.timeout(900)
async def test_retrieval_eval_gate():
    skip_if_milvus_empty()

    hits5: list[float] = []
    ndcgs: list[float] = []
    rrs: list[float] = []
    cprecs: list[float] = []
    crecs: list[float] = []
    rows: list[str] = []

    for query, domain, substrs in EVAL_GOLDENS:
        result = await query_rag(query, domain=domain, top_k=_TOP_K)
        titles = [r["title"] for r in result["results"]]
        rels = _relevance(titles, substrs)

        h5 = hit_at_k(rels, 5)
        nd = ndcg_at_k(rels, _TOP_K)
        rr = reciprocal_rank(rels, _TOP_K)
        # §17.794 — RAGAS deterministic pair. Each golden is single-target →
        # n_relevant=1 (recall == "did we retrieve it"); precision == AP.
        cp = context_precision(rels)
        cr = context_recall(rels, n_relevant=1)
        hits5.append(h5)
        ndcgs.append(nd)
        rrs.append(rr)
        cprecs.append(cp)
        crecs.append(cr)
        rows.append(
            f"  {domain:10s} hit@5={h5:.0f} ndcg@10={nd:.3f} rr={rr:.3f} "
            f"cprec={cp:.3f} crec={cr:.0f}  {query[:44]!r}"
        )

    m_hit5, m_ndcg, m_mrr = mean(hits5), mean(ndcgs), mean(rrs)
    m_cprec, m_crec = mean(cprecs), mean(crecs)
    report = (
        f"\n§17.430/§17.794 retrieval eval (n={len(EVAL_GOLDENS)}, top_k={_TOP_K}):\n"
        + "\n".join(rows)
        + f"\n  MEAN hit@5={m_hit5:.3f} nDCG@10={m_ndcg:.3f} MRR={m_mrr:.3f}"
        + f" ctx_precision={m_cprec:.3f} ctx_recall={m_crec:.3f}\n"
    )
    print(report)

    assert m_hit5 >= _FLOOR_MEAN_HIT_AT_5, f"mean hit@5 {m_hit5:.3f} < floor {_FLOOR_MEAN_HIT_AT_5}{report}"
    assert m_ndcg >= _FLOOR_MEAN_NDCG_AT_10, f"mean nDCG@10 {m_ndcg:.3f} < floor {_FLOOR_MEAN_NDCG_AT_10}{report}"
    assert m_mrr >= _FLOOR_MRR, f"MRR {m_mrr:.3f} < floor {_FLOOR_MRR}{report}"
    assert m_cprec >= _FLOOR_MEAN_CONTEXT_PRECISION, (
        f"mean context_precision {m_cprec:.3f} < floor {_FLOOR_MEAN_CONTEXT_PRECISION}{report}"
    )
    assert m_crec >= _FLOOR_MEAN_CONTEXT_RECALL, (
        f"mean context_recall {m_crec:.3f} < floor {_FLOOR_MEAN_CONTEXT_RECALL}{report}"
    )


@pytest.mark.timeout(900)
async def test_faithfulness_gate():
    """§17.794 — RAGAS faithfulness of answers GENERATED from retrieved context.

    Separate test from the (deterministic) ranking gate because it makes LLM
    calls: for a small subset of goldens, retrieve → generate an answer from the
    retrieved context → RAGAS-score the answer's groundedness. Fail-soft — the
    floor is asserted only over queries that actually scored; if the coaxed
    judge misses on all of them the sub-check skips rather than red-flagging a
    healthy retrieval pipeline. Local/dev only (needs Milvus + live models).
    """
    skip_if_milvus_empty()

    scores: list[float] = []
    rows: list[str] = []
    for query, domain, _substrs in EVAL_GOLDENS[:_FAITHFULNESS_SUBSET]:
        result = await query_rag(query, domain=domain, top_k=_TOP_K)
        score = await _faithfulness_for(query, result["results"])
        rows.append(f"  {domain:10s} faithfulness={score if score is None else f'{score:.3f}'}  {query[:44]!r}")
        if score is not None:
            scores.append(score)

    report = f"\n§17.794 faithfulness gate (subset={_FAITHFULNESS_SUBSET}):\n" + "\n".join(rows) + "\n"
    print(report)

    if not scores:
        pytest.skip("faithfulness judge scored no queries (coax miss) — retrieval gate unaffected")
    m_faith = mean(scores)
    print(f"  MEAN faithfulness={m_faith:.3f} over n={len(scores)}\n")
    assert m_faith >= _FLOOR_MEAN_FAITHFULNESS, (
        f"mean faithfulness {m_faith:.3f} < floor {_FLOOR_MEAN_FAITHFULNESS}{report}"
    )
