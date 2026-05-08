"""Retrieval quality scoring — runs golden queries through query_rag(), reports metrics.

Sprint W.8: standalone scripts don't run the app's lifespan startup, so the
http clients (Ollama, SearXNG, …) aren't initialized. Calling
``init_clients()`` at startup makes the script self-sufficient — no need to
go through the FastAPI lifespan.
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


@dataclass
class QueryResult:
    query: str
    expected_ids: list[str]
    retrieved_ids: list[str]
    recall_at_5: float
    recall_at_10: float
    mrr: float
    hit: bool


def _recall_at_k(expected: set[str], retrieved: list[str], k: int) -> float:
    if not expected:
        return 0.0
    top_k = set(retrieved[:k])
    return len(expected & top_k) / len(expected)


def _mrr(expected: set[str], retrieved: list[str]) -> float:
    for idx, eid in enumerate(retrieved, start=1):
        if eid in expected:
            return 1.0 / idx
    return 0.0


async def score_query(item: dict, top_k: int = 10) -> QueryResult:
    query = item["query"]
    expected_ids = item.get("expected_entry_ids", [])
    expected_set = set(expected_ids)

    results = await query_rag(query=query, top_k=top_k, domain=item.get("domain", "eng"))
    retrieved_ids = [r.get("entry_id", "") for r in results.get("results", [])]

    return QueryResult(
        query=query,
        expected_ids=expected_ids,
        retrieved_ids=retrieved_ids,
        recall_at_5=_recall_at_k(expected_set, retrieved_ids, 5),
        recall_at_10=_recall_at_k(expected_set, retrieved_ids, 10),
        mrr=_mrr(expected_set, retrieved_ids),
        hit=bool(expected_set & set(retrieved_ids)),
    )


async def run(golden_path: Path, output_path: Path) -> dict:
    init_clients()
    golden = json.loads(golden_path.read_text())["pairs"]
    results = [await score_query(item) for item in golden]

    summary = {
        "total_queries": len(results),
        "coverage": sum(1 for r in results if r.hit) / len(results) if results else 0.0,
        "mean_recall_at_5": mean(r.recall_at_5 for r in results) if results else 0.0,
        "mean_recall_at_10": mean(r.recall_at_10 for r in results) if results else 0.0,
        "mean_mrr": mean(r.mrr for r in results) if results else 0.0,
        "per_query": [asdict(r) for r in results],
    }

    output_path.write_text(json.dumps(summary, indent=2))
    return summary


def _print_report(summary: dict) -> None:
    print("=" * 60)
    print("Retrieval Quality Report")
    print("=" * 60)
    print(f"Queries:           {summary['total_queries']}")
    print(f"Coverage:          {summary['coverage']:.1%}")
    print(f"Mean Recall@5:     {summary['mean_recall_at_5']:.3f}")
    print(f"Mean Recall@10:    {summary['mean_recall_at_10']:.3f}")
    print(f"Mean MRR:          {summary['mean_mrr']:.3f}")
    print("=" * 60)
    misses = [r for r in summary["per_query"] if not r["hit"]]
    if misses:
        print(f"\n{len(misses)} missed queries:")
        for r in misses[:10]:
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
