"""Retrieval quality scoring — runs golden queries through query_rag(), reports metrics.

Sprint W.8: standalone scripts don't run the app's lifespan startup, so the
http clients (Ollama, SearXNG, …) aren't initialized. Calling
``init_clients()`` at startup makes the script self-sufficient — no need to
go through the FastAPI lifespan.

§17.230 — assertion shape switched from exact ``entry_id`` matching to
title-substring matching. Topic-mode ingest produces non-deterministic
``entry_id`` slugs across re-ingestion (each title gets a fresh ``-<8 char
hash>`` suffix; see §17.211's archaeology), so any score driven by
``expected_entry_ids`` collapses to 0 after a corpus rebuild (the §17.229
outcome). ``expected_titles_contain`` is a list of case-insensitive
substrings that ALL must appear in a retrieved title for that title to
count as a hit — discriminative enough to disambiguate (``["Kahn",
"algorithm"]`` won't false-positive on a generic algorithms doc) but
robust to title rewording across re-ingestion.

The old exact-id metric is preserved in the output as ``exact_id_*`` so
operators can see the delta against pre-§17.230 baselines without
re-running.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.rag_pipeline import query_rag
from app.utils.http_clients import init_clients
from app.utils.retrieval_metrics import context_precision, context_recall


# ---------------------------------------------------------------------------
# Title-substring matching (§17.230)
# ---------------------------------------------------------------------------

def _title_matches(title: str, expected_substrs: list[str]) -> bool:
    """True iff every substring appears in title (case-insensitive AND).

    Empty list returns False — an entry with no expected substrings is
    not a meaningful match target.
    """
    if not expected_substrs:
        return False
    t = title.lower()
    return all(s.lower() in t for s in expected_substrs)


def _title_hit_at_k(expected_substrs: list[str], titles: list[str], k: int) -> bool:
    return any(_title_matches(t, expected_substrs) for t in titles[:k])


def _title_mrr(expected_substrs: list[str], titles: list[str]) -> float:
    for idx, t in enumerate(titles, start=1):
        if _title_matches(t, expected_substrs):
            return 1.0 / idx
    return 0.0


@dataclass
class QueryResult:
    query: str
    expected_titles: list[str]
    expected_entry_ids: list[str]
    retrieved_titles: list[str]
    retrieved_ids: list[str]
    title_hit_at_5: bool
    title_hit_at_10: bool
    title_mrr: float
    exact_id_hit: bool
    # §17.794 — RAGAS context precision / recall over the title-substring
    # relevance (deterministic; no LLM). ``n_relevant`` is the labelled
    # ground-truth target count for this query.
    context_precision: float
    context_recall: float
    n_relevant: int
    # §17.794 — RAGAS faithfulness of a generated answer against the retrieved
    # context. Populated only with --faithfulness (LLM call per query); None
    # otherwise, and None on a scorer miss (fail-soft, see faithfulness.py).
    faithfulness: float | None = None
    # §17.798 — citation faithfulness (per-citation ATTRIBUTION). Generate a
    # CITE-AWARE answer over numbered sources, then score whether each `[n]`
    # cites a source that actually supports it. Only with --citation-faithfulness;
    # None otherwise and None on a scorer miss (fail-soft).
    citation_faithfulness: float | None = None


def _relevance_vector(expected_substrs: list[str], titles: list[str]) -> list[bool]:
    """Per-retrieved-title binary relevance (title contains ALL substrings)."""
    return [_title_matches(t, expected_substrs) for t in titles]


def _n_relevant(item: dict) -> int:
    """Ground-truth relevant-target count for context recall's denominator.

    Uses the number of labelled ``expected_entry_ids`` when present (multi-doc
    queries carry 2-3), else 1 for a single-target golden. The corpus goldens
    (§17.230) leave ``expected_entry_ids`` empty, so those default to 1 — where
    context recall reduces to hit-any (documented in retrieval_metrics.py).
    """
    return max(1, len(item.get("expected_entry_ids", []) or []))


_ANSWER_SYSTEM = (
    "You are a precise technical assistant. Answer the question using ONLY the "
    "provided context. Be concise (2-4 sentences). If the context does not "
    "cover the question, say so rather than inventing an answer."
)
_FAITHFULNESS_CONTEXT_CHARS = 6_000
_FAITHFULNESS_TOP_K = 5


def _build_context(rows: list[dict], k: int = _FAITHFULNESS_TOP_K) -> str:
    """Join the top-k retrieved contents into a single context block."""
    chunks = []
    for r in rows[:k]:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        if content:
            chunks.append(f"[{title}]\n{content}" if title else content)
    return "\n\n".join(chunks)[:_FAITHFULNESS_CONTEXT_CHARS]


async def _score_faithfulness(query: str, rows: list[dict]) -> float | None:
    """query_rag context → generate an answer → RAGAS faithfulness of it.

    Fail-soft: any empty/None at either step yields None (unscored), mirroring
    faithfulness.py's contract so a scorer miss never crashes the run.
    """
    # Local imports: only the --faithfulness path pulls in the LLM stack.
    from app import model_router
    from app.modules.faithfulness import score_faithfulness

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


def _build_numbered_sources(rows: list[dict], k: int = _FAITHFULNESS_TOP_K) -> list[str]:
    """Top-k retrieved contents as a 1-indexed source list (source [n] = [n-1])."""
    out = []
    for r in rows[:k]:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        if content:
            out.append(f"{title}\n{content}" if title else content)
    return out


async def _score_citation_faithfulness(query: str, rows: list[dict]) -> float | None:
    """§17.798 — generate a CITE-AWARE answer over numbered sources, then score
    per-citation attribution. Fail-soft (None at any empty/miss step)."""
    from app import model_router
    from app.modules.citation_faithfulness import (
        CITE_ANSWER_SYSTEM,
        score_citation_faithfulness,
    )

    sources = _build_numbered_sources(rows)
    if not sources:
        return None
    numbered = "\n\n".join(f"[{i}] {s}" for i, s in enumerate(sources, start=1))
    resp = await model_router.generate(
        prompt=f"Question: {query}\n\nSOURCES:\n{numbered}",
        role="model_general",
        system=CITE_ANSWER_SYSTEM,
        temperature=0.0,
        max_tokens=512,
    )
    answer = (getattr(resp, "text", "") or "").strip()
    if not answer:
        return None
    scored = await score_citation_faithfulness(answer, sources)
    return scored["score"] if scored else None


async def score_query(
    item: dict,
    top_k: int = 10,
    faithfulness: bool = False,
    citation_faithfulness: bool = False,
) -> QueryResult:
    query = item["query"]
    expected_titles = item.get("expected_titles_contain", [])
    expected_ids = item.get("expected_entry_ids", [])

    results = await query_rag(query=query, top_k=top_k, domain=item.get("domain", "eng"))
    rows = results.get("results", [])
    retrieved_titles = [r.get("title", "") for r in rows]
    retrieved_ids = [r.get("entry_id", "") for r in rows]

    rels = _relevance_vector(expected_titles, retrieved_titles)
    n_rel = _n_relevant(item)
    faith = await _score_faithfulness(query, rows) if faithfulness else None
    cite_faith = await _score_citation_faithfulness(query, rows) if citation_faithfulness else None

    return QueryResult(
        query=query,
        expected_titles=expected_titles,
        expected_entry_ids=expected_ids,
        retrieved_titles=retrieved_titles,
        retrieved_ids=retrieved_ids,
        title_hit_at_5=_title_hit_at_k(expected_titles, retrieved_titles, 5),
        title_hit_at_10=_title_hit_at_k(expected_titles, retrieved_titles, 10),
        title_mrr=_title_mrr(expected_titles, retrieved_titles),
        exact_id_hit=bool(set(expected_ids) & set(retrieved_ids)),
        context_precision=context_precision(rels),
        context_recall=context_recall(rels, n_rel),
        n_relevant=n_rel,
        faithfulness=faith,
        citation_faithfulness=cite_faith,
    )


async def run(
    golden_path: Path,
    output_path: Path,
    faithfulness: bool = False,
    citation_faithfulness: bool = False,
) -> dict:
    init_clients()
    golden = json.loads(golden_path.read_text())["pairs"]
    results = [
        await score_query(
            item, faithfulness=faithfulness, citation_faithfulness=citation_faithfulness
        )
        for item in golden
    ]
    n = len(results)

    # §17.794 — faithfulness is fail-soft (None on a scorer miss); average only
    # over the queries that actually scored so a few misses don't skew it.
    faith_scored = [r.faithfulness for r in results if r.faithfulness is not None]
    # §17.798 — same fail-soft averaging for citation faithfulness.
    cite_scored = [r.citation_faithfulness for r in results if r.citation_faithfulness is not None]

    summary = {
        "schema": "title_substring_v1",
        "total_queries": n,
        "coverage_at_5": sum(1 for r in results if r.title_hit_at_5) / n if n else 0.0,
        "coverage_at_10": sum(1 for r in results if r.title_hit_at_10) / n if n else 0.0,
        "mean_title_mrr": mean(r.title_mrr for r in results) if n else 0.0,
        "exact_id_coverage": sum(1 for r in results if r.exact_id_hit) / n if n else 0.0,
        # §17.794 — RAGAS metrics
        "mean_context_precision": mean(r.context_precision for r in results) if n else 0.0,
        "mean_context_recall": mean(r.context_recall for r in results) if n else 0.0,
        "mean_faithfulness": mean(faith_scored) if faith_scored else None,
        "faithfulness_scored": len(faith_scored),
        # §17.798 — RAGAS-adjacent citation (attribution) faithfulness
        "mean_citation_faithfulness": mean(cite_scored) if cite_scored else None,
        "citation_faithfulness_scored": len(cite_scored),
        "per_query": [asdict(r) for r in results],
    }

    output_path.write_text(json.dumps(summary, indent=2))
    return summary


def _print_report(summary: dict) -> None:
    print("=" * 60)
    print("Retrieval Quality Report (§17.230 — title-substring matching)")
    print("=" * 60)
    print(f"Queries:              {summary['total_queries']}")
    print(f"Coverage @5:          {summary['coverage_at_5']:.1%}")
    print(f"Coverage @10:         {summary['coverage_at_10']:.1%}")
    print(f"Mean MRR (title):     {summary['mean_title_mrr']:.3f}")
    print(f"Context precision:    {summary['mean_context_precision']:.3f}  (RAGAS, §17.794)")
    print(f"Context recall:       {summary['mean_context_recall']:.3f}  (RAGAS, §17.794)")
    mf = summary.get("mean_faithfulness")
    if mf is not None:
        print(f"Faithfulness:         {mf:.3f}  (RAGAS, n={summary['faithfulness_scored']})")
    else:
        print(f"Faithfulness:         n/a  (pass --faithfulness to score)")
    mcf = summary.get("mean_citation_faithfulness")
    if mcf is not None:
        print(f"Citation faithfulness:{mcf:.3f}  (attribution, §17.798, n={summary['citation_faithfulness_scored']})")
    else:
        print(f"Citation faithfulness:n/a  (pass --citation-faithfulness to score)")
    print(f"Exact-id coverage:    {summary['exact_id_coverage']:.1%}  (archival — see §17.229)")
    print("=" * 60)
    misses = [r for r in summary["per_query"] if not r["title_hit_at_10"]]
    if misses:
        print(f"\n{len(misses)} queries missed at top-10:")
        for r in misses[:15]:
            print(f"  - {r['query'][:70]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--golden", default="tests/fixtures/golden_set.json", type=Path)
    parser.add_argument("--output", default="retrieval_report.json", type=Path)
    parser.add_argument(
        "--faithfulness",
        action="store_true",
        help="Also score RAGAS faithfulness (LLM call per query: generate an "
        "answer from the retrieved context, then judge its groundedness). "
        "Off by default — the deterministic metrics stay cheap.",
    )
    parser.add_argument(
        "--citation-faithfulness",
        action="store_true",
        help="Also score citation (attribution) faithfulness (§17.798: generate "
        "a CITE-AWARE answer over numbered sources, then judge whether each "
        "[n] cites a source that actually supports it). LLM calls per query; "
        "off by default.",
    )
    args = parser.parse_args()

    if not args.golden.exists():
        print(f"ERROR: golden set not found at {args.golden}", file=sys.stderr)
        return 1

    summary = asyncio.run(run(
        args.golden, args.output,
        faithfulness=args.faithfulness,
        citation_faithfulness=args.citation_faithfulness,
    ))
    _print_report(summary)
    print(f"\nFull report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
