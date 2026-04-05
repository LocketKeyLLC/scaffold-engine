#!/usr/bin/env python3
"""Retrieval evaluation script for Scaffold Engine RAG pipeline.

Loads ground_truth.json, queries the /rag endpoint, and computes:
  - MRR  (Mean Reciprocal Rank)
  - Hit@3
  - Domain Purity

Usage (inside container):
    python tests/eval_retrieval.py

From host:
    make eval

Environment variables:
    SCAFFOLD_API_URL   — orchestrator base URL  (default: http://localhost:8000)
    SCAFFOLD_API_KEY   — API key for auth        (default: from API_KEY env)
    GROUND_TRUTH_PATH  — path to ground_truth.json (default: tests/ground_truth.json)
    EVAL_RESULTS_PATH  — output path for JSON results (default: tests/eval_results.json)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Baselines (from smokieRAGs eval — 2026-03-29)
# ---------------------------------------------------------------------------
BASELINES = {
    "mrr": 0.986,
    "hit_at_3": 1.000,
    "domain_purity": 1.000,
}

VALID_DOMAINS = {"prompt", "rag", "eng", "llm", "spec"}

# Tolerance for pass/fail comparison against baselines
TOLERANCE = 0.01


# ---------------------------------------------------------------------------
# Metric calculations (pure functions — importable for unit testing)
# ---------------------------------------------------------------------------

def compute_mrr(results_by_query: list[dict]) -> float:
    """Mean Reciprocal Rank.

    Each entry: {"expected_doc_ids": set[str], "retrieved_doc_ids": list[str]}
    """
    if not results_by_query:
        return 0.0

    reciprocal_ranks = []
    for entry in results_by_query:
        expected = entry["expected_doc_ids"]
        retrieved = entry["retrieved_doc_ids"]
        rr = 0.0
        for rank, doc_id in enumerate(retrieved, 1):
            if doc_id in expected:
                rr = 1.0 / rank
                break
        reciprocal_ranks.append(rr)

    return sum(reciprocal_ranks) / len(reciprocal_ranks)


def compute_hit_at_k(results_by_query: list[dict], k: int = 3) -> float:
    """Fraction of queries where at least one relevant doc appears in top-k.

    Each entry: {"expected_doc_ids": set[str], "retrieved_doc_ids": list[str]}
    """
    if not results_by_query:
        return 0.0

    hits = 0
    for entry in results_by_query:
        expected = entry["expected_doc_ids"]
        top_k = entry["retrieved_doc_ids"][:k]
        if any(doc_id in expected for doc_id in top_k):
            hits += 1

    return hits / len(results_by_query)


def compute_domain_purity(domain_results: list[dict]) -> float:
    """Average fraction of results matching the expected domain.

    Each entry: {"expected_domain": str, "retrieved_domains": list[str]}
    Queries with no results count as pure (nothing wrong returned).
    """
    if not domain_results:
        return 0.0

    purities = []
    for entry in domain_results:
        expected = entry["expected_domain"]
        retrieved = entry["retrieved_domains"]
        if not retrieved:
            purities.append(1.0)
            continue
        matching = sum(1 for d in retrieved if d == expected)
        purities.append(matching / len(retrieved))

    return sum(purities) / len(purities)


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only — no external deps)
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict, api_key: str = "", timeout: int = 60) -> dict:
    """POST JSON to a URL and return parsed response."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def run_eval(ground_truth_path: str, base_url: str, api_key: str) -> tuple[dict, list]:
    """Run full evaluation and return (metrics_dict, errors_list)."""
    with open(ground_truth_path) as f:
        gt = json.load(f)

    queries = gt["queries"]
    print(f"  Ground truth:  {ground_truth_path}")
    print(f"  Queries:       {len(queries)}")
    print(f"  Endpoint:      {base_url}/rag")
    print()

    retrieval_data: list[dict] = []   # For MRR / Hit@3
    domain_data: list[dict] = []      # For Domain Purity
    negative_data: list[dict] = []    # For negative query tracking
    errors: list[dict] = []
    per_query: list[dict] = []        # Detailed per-query results

    t0 = time.monotonic()

    for q in queries:
        qid = q["query_id"]
        text = q["text"]
        expected_docs = {d["doc_id"] for d in q["expected_docs"]}
        expected_domain = q.get("domain", "")

        try:
            result = _post_json(f"{base_url}/rag", {"query": text, "top_k": 10}, api_key)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            errors.append({"query_id": qid, "error": str(e)})
            print(f"  [{qid}] ERROR: {e}")
            continue
        except Exception as e:
            errors.append({"query_id": qid, "error": str(e)})
            print(f"  [{qid}] ERROR: {e}")
            continue

        if result.get("status") != "ok":
            errors.append({"query_id": qid, "error": result.get("error", "unknown")})
            print(f"  [{qid}] API error: {result.get('error')}")
            continue

        retrieved_ids = [r["entry_id"] for r in result.get("results", [])]
        retrieved_domains = [r["domain"] for r in result.get("results", [])]

        if expected_docs:
            # Positive query — compute rank of first relevant doc
            first_rank = None
            for rank, rid in enumerate(retrieved_ids, 1):
                if rid in expected_docs:
                    first_rank = rank
                    break

            hit_ids = [rid for rid in retrieved_ids if rid in expected_docs]

            retrieval_data.append({
                "expected_doc_ids": expected_docs,
                "retrieved_doc_ids": retrieved_ids,
            })

            # Domain purity (single-domain queries only)
            if expected_domain in VALID_DOMAINS:
                domain_data.append({
                    "expected_domain": expected_domain,
                    "retrieved_domains": retrieved_domains,
                })

            status = f"rank={first_rank}" if first_rank else "MISS"
            print(f"  [{qid}] {status:<10} hits={len(hit_ids)}/{len(expected_docs)}  results={len(retrieved_ids)}")

            per_query.append({
                "query_id": qid,
                "query": text,
                "first_rank": first_rank,
                "hits": len(hit_ids),
                "expected": len(expected_docs),
                "retrieved": len(retrieved_ids),
            })
        else:
            # Negative query
            negative_data.append({
                "query_id": qid,
                "result_count": len(retrieved_ids),
            })
            print(f"  [{qid}] negative   results={len(retrieved_ids)}")

    elapsed = time.monotonic() - t0

    # Compute metrics
    mrr = compute_mrr(retrieval_data)
    hit3 = compute_hit_at_k(retrieval_data, k=3)
    purity = compute_domain_purity(domain_data)

    metrics = {
        "mrr": round(mrr, 4),
        "hit_at_3": round(hit3, 4),
        "domain_purity": round(purity, 4),
        "queries_evaluated": len(retrieval_data),
        "domain_queries_evaluated": len(domain_data),
        "negative_queries": len(negative_data),
        "errors": len(errors),
        "elapsed_seconds": round(elapsed, 1),
    }

    return metrics, errors


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(metrics: dict, errors: list[dict]) -> int:
    """Print formatted evaluation report. Returns exit code (0=pass, 1=fail)."""
    print()
    print("=" * 62)
    print("  SCAFFOLD ENGINE — RETRIEVAL EVALUATION")
    print("=" * 62)

    print(f"\n  Positive queries:   {metrics['queries_evaluated']}")
    print(f"  Domain queries:     {metrics['domain_queries_evaluated']}")
    print(f"  Negative queries:   {metrics['negative_queries']}")
    print(f"  Errors:             {metrics['errors']}")
    print(f"  Elapsed:            {metrics['elapsed_seconds']}s")

    print(f"\n  {'Metric':<20} {'Score':>8} {'Baseline':>10} {'Delta':>8} {'':>4}")
    print(f"  {'-' * 20} {'-' * 8} {'-' * 10} {'-' * 8} {'-' * 4}")

    all_pass = True
    for key, label in [("mrr", "MRR"), ("hit_at_3", "Hit@3"), ("domain_purity", "Domain Purity")]:
        score = metrics[key]
        baseline = BASELINES[key]
        delta = score - baseline
        passed = delta >= -TOLERANCE
        if not passed:
            all_pass = False
        icon = "PASS" if passed else "FAIL"
        print(f"  {label:<20} {score:>8.4f} {baseline:>10.4f} {delta:>+8.4f} {icon:>4}")

    print(f"\n  {'PASSED' if all_pass else 'FAILED'}")
    print("=" * 62)

    if errors:
        print("\n  Errors:")
        for e in errors:
            print(f"    [{e['query_id']}] {e['error']}")

    return 0 if all_pass else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    gt_path = os.environ.get("GROUND_TRUTH_PATH", "tests/ground_truth.json")
    base_url = os.environ.get("SCAFFOLD_API_URL", "http://localhost:8000")
    api_key = os.environ.get("SCAFFOLD_API_KEY", "") or os.environ.get("API_KEY", "")

    # Resolve ground truth path
    if not Path(gt_path).exists():
        alt = Path(__file__).parent / "ground_truth.json"
        if alt.exists():
            gt_path = str(alt)
        else:
            print(f"ERROR: ground_truth.json not found at {gt_path}")
            sys.exit(1)

    print()
    print("=" * 62)
    print("  SCAFFOLD ENGINE — RETRIEVAL EVAL")
    print("=" * 62)
    print()

    metrics, errors = run_eval(gt_path, base_url, api_key)
    exit_code = print_report(metrics, errors)

    # Write results to JSON for CI / comparison
    results_path = os.environ.get("EVAL_RESULTS_PATH", "tests/eval_results.json")
    try:
        with open(results_path, "w") as f:
            json.dump(
                {"metrics": metrics, "errors": errors, "baselines": BASELINES},
                f,
                indent=2,
            )
        print(f"\n  Results saved to {results_path}")
    except OSError as e:
        print(f"\n  Warning: could not save results to {results_path}: {e}")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
