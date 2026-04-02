#!/usr/bin/env python3
"""Ground-truth retrieval evaluation for Scaffold Engine.

Loads ground_truth.json, embeds queries via Ollama, searches Milvus,
computes Precision@3, MRR, Hit Rate@3, and a 5×5 domain confusion matrix.

Supports .npz embedding cache for deterministic reruns.

Usage:
    python3 eval_retrieval.py                    # full run
    python3 eval_retrieval.py --no-cache         # force re-embed
    python3 eval_retrieval.py --domain rag       # filter to one domain
    python3 eval_retrieval.py --type ambiguous   # filter to query type
"""
import json
import sys
import hashlib
import time
import argparse
from pathlib import Path
from collections import defaultdict

import numpy as np
import requests
from pymilvus import MilvusClient

# ── Config ──────────────────────────────────────────────────────────────
FIXTURE_PATH = Path(__file__).parent / "ground_truth.json"
CACHE_DIR = Path(__file__).parent / ".eval_cache"
OLLAMA_URL = "http://172.18.0.1:11434"
MILVUS_URI = "http://milvus-standalone:19530"
COLLECTION = "technical_knowledge"
EMBED_MODEL = "qwen3-embedding:8b"
EMBED_DIM = 4096
TOP_K = 5  # retrieve 5 but evaluate at k=3
DOMAINS = ["prompt", "rag", "eng", "llm", "spec"]

# ── Thresholds ──────────────────────────────────────────────────────────
THRESHOLDS = {
    "precision_at_3": {"healthy": 0.70, "broken": 0.50},
    "mrr": {"healthy": 0.75, "broken": 0.55},
    "hit_rate_at_3": {"healthy": 0.90, "broken": 0.75},
    "domain_purity": {"healthy": 0.75, "broken": 0.60},
}


def load_fixture(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def get_model_hash() -> str:
    """Hash the Ollama model digest for cache invalidation."""
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/show", json={"name": EMBED_MODEL})
        digest = resp.json().get("digest", EMBED_MODEL)
        return hashlib.sha256(digest.encode()).hexdigest()[:12]
    except Exception:
        return hashlib.sha256(EMBED_MODEL.encode()).hexdigest()[:12]


def embed_query(text: str) -> list[float]:
    """Embed a single query via Ollama."""
    resp = requests.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": text},
    )
    resp.raise_for_status()
    data = resp.json()
    return data["embeddings"][0]


def load_or_build_cache(queries: list[dict], force: bool = False) -> dict:
    """Load cached embeddings or build them. Returns {query_id: np.ndarray}."""
    CACHE_DIR.mkdir(exist_ok=True)
    model_hash = get_model_hash()
    cache_file = CACHE_DIR / f"embeddings_{model_hash}.npz"

    if not force and cache_file.exists():
        data = np.load(cache_file)
        cached = {k: data[k] for k in data.files}
        query_ids = {q["query_id"] for q in queries}
        if query_ids.issubset(cached.keys()):
            print(f"  Cache hit: {cache_file.name} ({len(cached)} vectors)")
            return cached
        print(f"  Cache partial: {len(cached & query_ids)}/{len(query_ids)} — rebuilding")

    print(f"  Embedding {len(queries)} queries via {EMBED_MODEL}...")
    embeddings = {}
    for i, q in enumerate(queries):
        vec = embed_query(q["text"])
        embeddings[q["query_id"]] = np.array(vec, dtype=np.float32)
        if (i + 1) % 10 == 0:
            print(f"    {i + 1}/{len(queries)} embedded")

    np.savez_compressed(cache_file, **embeddings)
    print(f"  Cached → {cache_file.name}")
    return embeddings


def search_milvus(client: MilvusClient, vector: list, domain: str = None) -> list[dict]:
    """Search Milvus with optional domain filter. Returns list of {entry_id, domain, score}."""
    search_params = {"metric_type": "L2", "params": {"ef": 64}}

    filter_expr = None
    if domain and domain not in ("cross_domain", "out_of_domain"):
        filter_expr = f'domain == "{domain}"'

    results = client.search(
        collection_name=COLLECTION,
        data=[vector],
        limit=TOP_K,
        search_params=search_params,
        filter=filter_expr,
        output_fields=["entry_id", "domain"],
    )

    hits = []
    for hit in results[0]:
        hits.append({
            "entry_id": hit["entity"]["entry_id"],
            "domain": hit["entity"]["domain"],
            "l2_distance": hit["distance"],
        })
    return hits


# ── Metrics ─────────────────────────────────────────────────────────────

def precision_at_k(retrieved: list[str], relevant: set, k: int = 3) -> float:
    return sum(1 for doc in retrieved[:k] if doc in relevant) / k


def reciprocal_rank(retrieved: list[str], relevant: set) -> float:
    for i, doc in enumerate(retrieved):
        if doc in relevant:
            return 1.0 / (i + 1)
    return 0.0


def hit_at_k(retrieved: list[str], relevant: set, k: int = 3) -> float:
    return 1.0 if any(doc in relevant for doc in retrieved[:k]) else 0.0


def grade(value: float, metric_name: str) -> str:
    t = THRESHOLDS[metric_name]
    if value >= t["healthy"]:
        return "✅"
    elif value >= t["broken"]:
        return "⚠️"
    else:
        return "❌"


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Evaluate RAG retrieval against ground truth")
    parser.add_argument("--no-cache", action="store_true", help="Force re-embedding")
    parser.add_argument("--domain", type=str, help="Filter to specific domain")
    parser.add_argument("--type", type=str, dest="qtype", help="Filter to query type")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show per-query results")
    args = parser.parse_args()

    # Load fixture
    fixture = load_fixture(FIXTURE_PATH)
    queries = fixture["queries"]
    print(f"Loaded {len(queries)} queries from {FIXTURE_PATH.name}")

    # Filter if requested
    if args.domain:
        queries = [q for q in queries if q["domain"] == args.domain]
        print(f"  Filtered to domain={args.domain}: {len(queries)} queries")
    if args.qtype:
        queries = [q for q in queries if q["query_type"] == args.qtype]
        print(f"  Filtered to type={args.qtype}: {len(queries)} queries")

    if not queries:
        print("No queries match filters. Exiting.")
        sys.exit(1)

    # Build / load embeddings
    embeddings = load_or_build_cache(queries, force=args.no_cache)

    # Connect to Milvus
    client = MilvusClient(uri=MILVUS_URI)
    print(f"Connected to Milvus: {MILVUS_URI}")

    # ── Evaluate ────────────────────────────────────────────────────
    p3_scores = []
    mrr_scores = []
    hit3_scores = []
    confusion = defaultdict(lambda: defaultdict(int))  # [query_domain][retrieved_domain]
    per_type = defaultdict(lambda: {"p3": [], "mrr": [], "hit3": []})
    failures = []

    t0 = time.time()

    for q in queries:
        qid = q["query_id"]
        vec = embeddings[qid].tolist()

        # For domain-scoped queries, search within domain; for cross/out/negative, search all
        search_domain = q["domain"] if q["domain"] in DOMAINS else None
        hits = search_milvus(client, vec, domain=search_domain)

        retrieved_ids = [h["entry_id"] for h in hits]
        relevant_ids = {d["doc_id"] for d in q["expected_docs"] if d["relevance"] >= 1}

        p3 = precision_at_k(retrieved_ids, relevant_ids, k=3)
        rr = reciprocal_rank(retrieved_ids, relevant_ids)
        h3 = hit_at_k(retrieved_ids, relevant_ids, k=3)

        p3_scores.append(p3)
        mrr_scores.append(rr)
        hit3_scores.append(h3)

        qt = q["query_type"]
        per_type[qt]["p3"].append(p3)
        per_type[qt]["mrr"].append(rr)
        per_type[qt]["hit3"].append(h3)

        # Confusion matrix: only for non-negative queries
        if q["domain"] != "out_of_domain":
            query_domain = q["domain"] if q["domain"] in DOMAINS else "cross"
            for h in hits[:3]:
                confusion[query_domain][h["domain"]] += 1

        # Track failures
        if rr == 0.0 and relevant_ids:
            failures.append({
                "query_id": qid,
                "text": q["text"],
                "domain": q["domain"],
                "type": qt,
                "expected": list(relevant_ids),
                "got": retrieved_ids[:3],
            })

        if args.verbose:
            status = "✅" if rr > 0 else ("⬜" if not relevant_ids else "❌")
            print(f"  {status} {qid} P@3={p3:.2f} RR={rr:.2f} | {q['text'][:60]}")
            if args.verbose and hits:
                for h in hits[:3]:
                    marker = "→" if h["entry_id"] in relevant_ids else " "
                    print(f"      {marker} L2={h['l2_distance']:.4f} {h['domain']:6s} {h['entry_id']}")

    elapsed = time.time() - t0

    # ── Aggregate Metrics ───────────────────────────────────────────
    mean_p3 = np.mean(p3_scores)
    mean_mrr = np.mean(mrr_scores)
    mean_hit3 = np.mean(hit3_scores)

    print(f"\n{'='*65}")
    print(f"  RETRIEVAL EVALUATION RESULTS  ({len(queries)} queries, {elapsed:.1f}s)")
    print(f"{'='*65}")
    print(f"  Precision@3:  {mean_p3:.4f}  {grade(mean_p3, 'precision_at_3')}")
    print(f"  MRR:          {mean_mrr:.4f}  {grade(mean_mrr, 'mrr')}")
    print(f"  Hit Rate@3:   {mean_hit3:.4f}  {grade(mean_hit3, 'hit_rate_at_3')}")

    # ── Per-Type Breakdown ──────────────────────────────────────────
    print(f"\n  By Query Type:")
    for qt in ["factual", "conceptual", "comparative", "multi_hop", "ambiguous", "negative"]:
        if qt not in per_type:
            continue
        tp = per_type[qt]
        n = len(tp["p3"])
        print(f"    {qt:12s}  n={n:2d}  P@3={np.mean(tp['p3']):.3f}  MRR={np.mean(tp['mrr']):.3f}  Hit@3={np.mean(tp['hit3']):.3f}")

    # ── Domain Confusion Matrix ─────────────────────────────────────
    print(f"\n  Domain Confusion Matrix (query domain → retrieved domain, top-3):")
    all_domains = DOMAINS + (["cross"] if "cross" in confusion else [])
    header = "            " + "  ".join(f"{d:>6s}" for d in DOMAINS)
    print(f"  {header}")
    purity_scores = []
    for qd in all_domains:
        total = sum(confusion[qd].values())
        if total == 0:
            continue
        row_vals = []
        for rd in DOMAINS:
            frac = confusion[qd][rd] / total if total > 0 else 0
            row_vals.append(frac)
        row_str = "  ".join(f"{v:6.2f}" for v in row_vals)
        print(f"  {qd:>10s}  {row_str}")
        if qd in DOMAINS:
            diag_idx = DOMAINS.index(qd)
            purity_scores.append(row_vals[diag_idx])

    if purity_scores:
        mean_purity = np.mean(purity_scores)
        print(f"\n  Domain Purity: {mean_purity:.4f}  {grade(mean_purity, 'domain_purity')}")

    # ── Failures ────────────────────────────────────────────────────
    if failures:
        print(f"\n  FAILURES ({len(failures)} queries with no relevant doc in top-{TOP_K}):")
        for f in failures:
            print(f"    {f['query_id']} [{f['type']}] {f['text'][:55]}")
            print(f"      Expected: {f['expected']}")
            print(f"      Got:      {f['got']}")

    # ── Health Summary ──────────────────────────────────────────────
    print(f"\n{'='*65}")
    all_ok = (
        mean_p3 >= THRESHOLDS["precision_at_3"]["healthy"]
        and mean_mrr >= THRESHOLDS["mrr"]["healthy"]
        and mean_hit3 >= THRESHOLDS["hit_rate_at_3"]["healthy"]
    )
    if all_ok:
        print("  ✅ ALL METRICS HEALTHY")
    else:
        print("  ⚠️  SOME METRICS BELOW THRESHOLD — review failures above")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
