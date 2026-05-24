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


async def score_query(item: dict, top_k: int = 10) -> QueryResult:
    query = item["query"]
    expected_titles = item.get("expected_titles_contain", [])
    expected_ids = item.get("expected_entry_ids", [])

    results = await query_rag(query=query, top_k=top_k, domain=item.get("domain", "eng"))
    rows = results.get("results", [])
    retrieved_titles = [r.get("title", "") for r in rows]
    retrieved_ids = [r.get("entry_id", "") for r in rows]

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
    )


async def run(golden_path: Path, output_path: Path) -> dict:
    init_clients()
    golden = json.loads(golden_path.read_text())["pairs"]
    results = [await score_query(item) for item in golden]
    n = len(results)

    summary = {
        "schema": "title_substring_v1",
        "total_queries": n,
        "coverage_at_5": sum(1 for r in results if r.title_hit_at_5) / n if n else 0.0,
        "coverage_at_10": sum(1 for r in results if r.title_hit_at_10) / n if n else 0.0,
        "mean_title_mrr": mean(r.title_mrr for r in results) if n else 0.0,
        "exact_id_coverage": sum(1 for r in results if r.exact_id_hit) / n if n else 0.0,
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
    args = parser.parse_args()

    if not args.golden.exists():
        print(f"ERROR: golden set not found at {args.golden}", file=sys.stderr)
        return 1

    summary = asyncio.run(run(args.golden, args.output))
    _print_report(summary)
    print(f"\nFull report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
