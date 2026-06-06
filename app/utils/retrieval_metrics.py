"""§17.430 — deterministic retrieval-quality metrics for the eval gate.

Pure, dependency-free ranking metrics so retrieval quality can be a regression
gate WITHOUT an LLM judge (unlike Ragas → cost/nondeterminism) and WITHOUT a
new C-extension dependency (unlike pytrec_eval). The math is small and is
unit-tested against known values in tests/test_retrieval_metrics.py.

`scripts/score_retrieval.py` already computes hit@k + MRR inline against the
substring goldens (§17.230); this module factors the math into reusable,
tested functions and adds nDCG@k, which the script lacked.

Input contract: each function takes ``rels`` — the binary relevance of the
RETRIEVED items in ranked order (``rels[0]`` is the top-ranked result).
Relevance is judged by the caller (for scaffold-engine: the result title
contains a labeled substring — robust to re-ingestion drift, see §17.211).
These metrics need only the ranked relevance of what was retrieved; they do
NOT require a full-corpus qrel set, which the project does not maintain.
"""
from __future__ import annotations

import math


def hit_at_k(rels: list[bool], k: int) -> float:
    """1.0 if any of the top-k retrieved items is relevant, else 0.0.

    Answers "did we surface a relevant doc in the top k?" — the retrieval
    success signal. Insensitive to where within the top-k it landed (see
    ``reciprocal_rank`` / ``ndcg_at_k`` for rank-sensitive measures).
    """
    return 1.0 if any(rels[:k]) else 0.0


def reciprocal_rank(rels: list[bool], k: int | None = None) -> float:
    """1 / rank of the first relevant item (rank is 1-indexed); 0.0 if none.

    The per-query term of MRR. ``k`` optionally restricts to the top-k window.
    """
    seq = rels[:k] if k is not None else rels
    for i, r in enumerate(seq):
        if r:
            return 1.0 / (i + 1)
    return 0.0


def dcg_at_k(rels: list[bool], k: int) -> float:
    """Binary-relevance DCG@k with the standard log2(rank+1) discount."""
    return sum(1.0 / math.log2(i + 2) for i, r in enumerate(rels[:k]) if r)


def ndcg_at_k(rels: list[bool], k: int) -> float:
    """nDCG@k over the retrieved list (binary relevance).

    IDCG is the DCG of the ideal ordering of the relevant items that were
    retrieved (all relevant pulled to the front), capped at k. So this
    measures how well the ranker ORDERED the relevant items it retrieved —
    a perfect ranking of whatever relevant docs are present scores 1.0.

    It deliberately does NOT penalize relevant docs missing from the corpus
    (that is what ``hit_at_k`` captures); pair the two for a full picture.
    Returns 0.0 when no relevant item is in the retrieved list.
    """
    dcg = dcg_at_k(rels, k)
    n_rel = min(sum(1 for r in rels if r), k)
    if n_rel == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(i + 2) for i in range(n_rel))
    return dcg / idcg if idcg > 0 else 0.0
