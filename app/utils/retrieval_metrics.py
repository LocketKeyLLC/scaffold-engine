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


# ---------------------------------------------------------------------------
# §17.794 — RAGAS context precision / recall (retrieval-label, no LLM judge)
#
# RAGAS (arXiv 2309.15217) defines context precision and context recall over
# the LLM's own relevance/attribution judgements. We compute the SAME formulas
# over the deterministic title-substring relevance the goldens already carry
# (see the module docstring's input contract) — no LLM, no nondeterminism, so
# they gate in CI exactly like hit@k / nDCG@k above. The LLM-judge third pillar,
# faithfulness, lives in app/modules/faithfulness.py (it needs a generated
# ANSWER, which these ranking metrics do not); the live gate pairs all three.
# ---------------------------------------------------------------------------


def context_precision(rels: list[bool]) -> float:
    """RAGAS Context Precision@K over the ranked retrieved list.

    ``CP@K = (Σ_k Precision@k · v_k) / (total relevant retrieved)`` where
    ``Precision@k`` is the precision of the top-k prefix and ``v_k`` is 1 iff
    the item at rank k is relevant — i.e. Average Precision. It rewards
    packing relevant items at the TOP of the list: a run of relevant docs at
    ranks 1..m scores 1.0, the same docs scattered lower scores less.

    Note the boundary cases this shares with the ranking family: with exactly
    one relevant item at rank r this collapses to ``1/r`` (== reciprocal_rank);
    the metric only diverges from MRR when a query has MULTIPLE relevant docs
    in the retrieved list (which the substring goldens do produce — e.g. a
    query whose label matches several near-duplicate entries). Returns 0.0 when
    nothing relevant was retrieved.
    """
    total_relevant = sum(1 for r in rels if r)
    if total_relevant == 0:
        return 0.0
    hits = 0
    weighted = 0.0
    for i, r in enumerate(rels):
        if r:
            hits += 1
            weighted += hits / (i + 1)  # precision@(i+1) at this relevant hit
    return weighted / total_relevant


def context_recall(rels: list[bool], n_relevant: int) -> float:
    """RAGAS (non-LLM) Context Recall — fraction of ground-truth relevant
    items that made it into the retrieved list.

    ``recall = min(1, |relevant retrieved| / n_relevant)``. ``n_relevant`` is
    the caller-supplied ground-truth count (the goldens do not maintain a
    full-corpus qrel set — see the module docstring). The ``min`` caps the
    ratio at 1.0 so a query whose label matches more near-duplicate rows than
    the labelled target count cannot exceed perfect recall.

    Boundary: with ``n_relevant == 1`` (the common single-target golden) this
    collapses to "was any relevant doc retrieved" (== ``hit_at_k`` over the
    whole list). It only carries information beyond hit@k when a query enumerates
    multiple distinct relevant targets. Returns 0.0 for ``n_relevant <= 0``.
    """
    if n_relevant <= 0:
        return 0.0
    retrieved_relevant = sum(1 for r in rels if r)
    return min(1.0, retrieved_relevant / n_relevant)
