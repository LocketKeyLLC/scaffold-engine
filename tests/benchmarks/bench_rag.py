#!/usr/bin/env python3
"""
Scaffold Engine — RAG retrieval micro-benchmark
================================================

Component-level benchmark for the retrieval pipeline. NO LLM call —
isolates query→embed→Milvus→rerank from generation latency.

Why a separate bench: bench_pipeline.py runs end-to-end (~43 min)
and produces one wall-clock number. If RAG retrieval slows from 200ms
to 800ms, that's invisible until the macro number creeps up. This
bench runs in seconds and surfaces RAG drift directly.

Phases:
  - cold: first call (embedder load, Milvus connect, reranker first batch)
  - warm: N subsequent calls (avg + p50/p95/p99 over N iterations)

§17.352 — per-stage decomposition. Each warm iteration now also calls
the retrieval stages directly (embed → vector_search ∥ keyword_search →
rrf_fuse → rerank) so per-stage latency is captured separately from the
aggregate. Drift in one stage (e.g. embedder slow, reranker batch ramp)
is visible BEFORE the aggregate creeps up. Schema bumped to 1.1; new
``summary.stage.*`` keys plus a ``rerank_per_pair_ms`` derived metric.

Output: append a JSONL record to tests/benchmarks/bench_rag_results.jsonl
with run_id, hardware, phases, and per-query timings. Same shape as
bench_pipeline so bench_compare can read both.

Usage:
    python tests/benchmarks/bench_rag.py
    # or:
    make bench-rag
    # iterations override:
    BENCH_ITERATIONS=20 python tests/benchmarks/bench_rag.py

Runs inside the orchestrator container (needs Milvus + Ollama + the
reranker singleton). Skips if any are unavailable rather than 500ing.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError:
    print("ERROR: psutil required. Install with: pip install psutil")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────────

ITERATIONS = int(os.getenv("BENCH_ITERATIONS", "10"))
TOP_K = int(os.getenv("BENCH_TOP_K", "5"))

# Fixed query set for reproducibility. Mix of short / long across the
# actual partitions present in toon_v2 — eng / llm / rag / prompt / spec.
# §17.352 — earlier list used "ml" / "infra" which aren't valid partition
# names (see VALID_DOMAINS in app/config.py); those queries returned 0
# hits and silently skewed rerank_per_pair_mean down toward zero by
# averaging in queries that never reranked.
QUERIES = [
    ("DAG orchestration", "eng"),
    ("retrieval augmented generation patterns", "rag"),
    ("transformer attention", "llm"),
    ("how does HNSW vector search work", "eng"),
    ("chain of thought prompting", "prompt"),
]

def _writable_results_file() -> Path:
    """Default to script dir; fall back to /tmp on read-only filesystem.

    The runtime container mounts ``/code`` read-only and only the dev
    image (or an explicit `tests/benchmarks` rw bind) can persist
    results to the repo. Falling back to /tmp keeps the bench usable
    in either mode — the operator can `docker cp` the output to the
    host if they want it on disk.
    """
    override = os.getenv("BENCH_RESULTS_FILE")
    if override:
        return Path(override)
    here = Path(__file__).parent / "bench_rag_results.jsonl"
    try:
        # Cheap writability probe — open append, immediately close.
        # No write happens unless we're already going to write later.
        with open(here, "a"):
            pass
        return here
    except OSError:
        fallback = Path("/tmp/scaffold-bench/bench_rag_results.jsonl")
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


RESULTS_FILE = _writable_results_file()


# ── Helpers ────────────────────────────────────────────────────────────────


def _hardware() -> dict:
    return {
        "cpu": platform.processor() or platform.machine(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "ram_total_mb": round(psutil.virtual_memory().total / 1024 / 1024),
        "platform": platform.platform(),
    }


def _percentiles(samples_ms: list[float]) -> dict:
    """Return p50/p95/p99 over a list of sample times in ms."""
    if not samples_ms:
        return {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0}
    s = sorted(samples_ms)
    n = len(s)
    # Linear interpolation between sorted samples; matches numpy quantile.
    def _pct(p: float) -> float:
        if n == 1:
            return s[0]
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return s[lo] * (1 - frac) + s[hi] * frac
    return {
        "p50_ms": round(_pct(0.50), 1),
        "p95_ms": round(_pct(0.95), 1),
        "p99_ms": round(_pct(0.99), 1),
    }


# ── Bench phases ───────────────────────────────────────────────────────────


async def _run_one_query(query: str, domain: str) -> tuple[float, int, str | None]:
    """Run query_rag. Returns (latency_ms, hits, reranker_backend or error)."""
    from app.modules.rag_pipeline import query_rag

    t0 = time.monotonic()
    try:
        result = await query_rag(query, domain=domain, top_k=TOP_K)
    except Exception as exc:
        return ((time.monotonic() - t0) * 1000, 0, f"error: {exc}")
    elapsed_ms = (time.monotonic() - t0) * 1000

    if result.get("status") == "error":
        return (elapsed_ms, 0, f"error: {result.get('error')}")
    hits = len(result.get("results") or [])
    backend = (result.get("metadata") or {}).get("reranker_backend") or "unknown"
    return (elapsed_ms, hits, backend)


async def _run_one_query_decomposed(query: str, domain: str) -> dict | None:
    """Per-stage timing: embed | (vector ∥ keyword) | fuse | rerank.

    Calls the private stage helpers directly so each stage is timed in
    isolation. Returns ``None`` on collection-unavailable / embed-failed
    so the caller can skip without polluting the warm-mean stats.

    ``search_parallel_ms`` is the wall-clock for ``asyncio.gather`` of
    vector + keyword; production runs these in parallel so the parallel
    number is what matters. ``vector_search_ms`` / ``keyword_search_ms``
    are the sequential-time-equivalents (sum-of-parts) for debugging
    which leg dominated.
    """
    from app.modules.rag_pipeline import (
        _embed_query, _vector_search, _keyword_search,
        _rrf_fuse, _rerank, _get_client,
    )

    loop = asyncio.get_running_loop()
    collection = await loop.run_in_executor(None, _get_client)
    if collection is None:
        return None

    t_embed = time.monotonic()
    query_embedding = await _embed_query(query, query_intent="general")
    embed_ms = (time.monotonic() - t_embed) * 1000
    if query_embedding is None:
        return None

    # Time the parallel gather (production behavior) AND each leg's
    # sequential equivalent. The sequential numbers are reconstructed
    # by running each helper a second time — small extra cost but lets
    # us see which leg is the long pole when search_parallel_ms drifts.
    t_search = time.monotonic()
    # §17.767 — the legs now return (results, failed_domains); unpack for fusion.
    (vector_results, _), (keyword_results, _) = await asyncio.gather(
        _vector_search(collection, query_embedding, TOP_K * 2, domain=domain),
        _keyword_search(collection, query, TOP_K * 2, domain=domain),
    )
    search_parallel_ms = (time.monotonic() - t_search) * 1000

    # Sequential-equivalent timings — useful only when the parallel
    # number looks wrong. Skipped when BENCH_RAG_SKIP_SEQ_TIMING is set.
    vector_seq_ms = 0.0
    keyword_seq_ms = 0.0
    if not os.getenv("BENCH_RAG_SKIP_SEQ_TIMING"):
        t_v = time.monotonic()
        await _vector_search(collection, query_embedding, TOP_K * 2, domain=domain)
        vector_seq_ms = (time.monotonic() - t_v) * 1000
        t_k = time.monotonic()
        await _keyword_search(collection, query, TOP_K * 2, domain=domain)
        keyword_seq_ms = (time.monotonic() - t_k) * 1000

    t_fuse = time.monotonic()
    fused = _rrf_fuse(vector_results, keyword_results)
    fuse_ms = (time.monotonic() - t_fuse) * 1000

    rerank_ms = 0.0
    rerank_pairs = 0
    rerank_backend = "skipped"
    if fused:
        rerank_pairs = len(fused)
        t_r = time.monotonic()
        _ranked, rerank_meta = await _rerank(query, fused, TOP_K)
        rerank_ms = (time.monotonic() - t_r) * 1000
        rerank_backend = rerank_meta.get("backend") or "unknown"

    rerank_per_pair_ms = (rerank_ms / rerank_pairs) if rerank_pairs else 0.0

    return {
        "embed_ms": round(embed_ms, 2),
        "search_parallel_ms": round(search_parallel_ms, 2),
        "vector_search_seq_ms": round(vector_seq_ms, 2),
        "keyword_search_seq_ms": round(keyword_seq_ms, 2),
        "fuse_ms": round(fuse_ms, 2),
        "rerank_ms": round(rerank_ms, 2),
        "rerank_pairs": rerank_pairs,
        "rerank_per_pair_ms": round(rerank_per_pair_ms, 2),
        "rerank_backend": rerank_backend,
        "vector_hits": len(vector_results),
        "keyword_hits": len(keyword_results),
    }


async def _bench_cold(query: str, domain: str) -> dict:
    """Single first-run query. Captures cold-start cost (model load,
    Milvus connect, reranker first batch). Don't average across cold
    runs — each subsequent query is warm."""
    elapsed_ms, hits, backend = await _run_one_query(query, domain)
    return {
        "query": query,
        "domain": domain,
        "latency_ms": round(elapsed_ms, 1),
        "hits": hits,
        "reranker_backend": backend,
    }


async def _bench_warm(query: str, domain: str, iterations: int) -> dict:
    """Run the same query N times. Returns per-iter samples + p50/p95/p99
    + mean for the aggregate, plus per-stage means (§17.352).

    The first iteration of this run is still warm because we invoked
    ``_bench_cold`` first. Per-stage timings come from
    ``_run_one_query_decomposed``; if any iteration returns None
    (collection unavailable mid-run), the stage stats degrade to 0 for
    that iteration only.
    """
    samples_ms: list[float] = []
    embed_samples: list[float] = []
    search_samples: list[float] = []
    rerank_samples: list[float] = []
    rerank_per_pair_samples: list[float] = []
    last_hits = 0
    last_backend = "unknown"
    last_rerank_pairs = 0
    for _ in range(iterations):
        elapsed_ms, hits, backend = await _run_one_query(query, domain)
        samples_ms.append(elapsed_ms)
        last_hits = hits
        last_backend = backend

        decomp = await _run_one_query_decomposed(query, domain)
        if decomp is not None:
            embed_samples.append(decomp["embed_ms"])
            search_samples.append(decomp["search_parallel_ms"])
            rerank_samples.append(decomp["rerank_ms"])
            if decomp["rerank_pairs"] > 0:
                rerank_per_pair_samples.append(decomp["rerank_per_pair_ms"])
                last_rerank_pairs = decomp["rerank_pairs"]

    mean_ms = statistics.mean(samples_ms) if samples_ms else 0
    return {
        "query": query,
        "domain": domain,
        "iterations": iterations,
        "hits": last_hits,
        "reranker_backend": last_backend,
        "mean_ms": round(mean_ms, 1),
        "min_ms": round(min(samples_ms), 1) if samples_ms else 0,
        "max_ms": round(max(samples_ms), 1) if samples_ms else 0,
        **_percentiles(samples_ms),
        "stage": {
            "embed_mean_ms": round(statistics.mean(embed_samples), 2) if embed_samples else 0,
            "search_parallel_mean_ms": round(statistics.mean(search_samples), 2) if search_samples else 0,
            "rerank_mean_ms": round(statistics.mean(rerank_samples), 2) if rerank_samples else 0,
            "rerank_per_pair_mean_ms": round(
                statistics.mean(rerank_per_pair_samples), 2
            ) if rerank_per_pair_samples else 0,
            "rerank_pairs_last": last_rerank_pairs,
        },
    }


# ── Main ───────────────────────────────────────────────────────────────────


async def main() -> None:
    # Standalone-script context doesn't hit FastAPI's lifespan, so the
    # shared HTTP clients (Ollama et al.) aren't pre-built. Calling
    # init_clients() here gives the bench the same client wiring a real
    # request handler would see. Idempotent — re-runs are no-ops.
    from app.utils.http_clients import init_clients
    init_clients()

    run_id = f"rag_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    started = datetime.now(timezone.utc).isoformat()
    print(f"[bench_rag] run_id={run_id} iterations={ITERATIONS} top_k={TOP_K}")
    print(f"[bench_rag] queries: {len(QUERIES)}")
    t0 = time.monotonic()

    cold_results = []
    warm_results = []

    for i, (query, domain) in enumerate(QUERIES, 1):
        print(f"[bench_rag] [{i}/{len(QUERIES)}] {query!r} (domain={domain})")
        # Cold pass: 1 query, no averaging — captures setup cost on first
        # query of the run; subsequent queries' cold rows just add latency
        # context for the warm phase.
        cold = await _bench_cold(query, domain)
        cold_results.append(cold)
        print(f"  cold: {cold['latency_ms']}ms, hits={cold['hits']}, backend={cold['reranker_backend']}")

        warm = await _bench_warm(query, domain, ITERATIONS)
        warm_results.append(warm)
        print(
            f"  warm ({ITERATIONS}× ): mean={warm['mean_ms']}ms "
            f"p50={warm['p50_ms']}ms p95={warm['p95_ms']}ms p99={warm['p99_ms']}ms"
        )

    total_s = time.monotonic() - t0

    # Aggregate across all warm samples for a single "system-level" line.
    all_warm_ms: list[float] = []
    for w in warm_results:
        # Reconstruct individual samples from per-query stats isn't possible
        # post-hoc; we only kept the percentiles. Use mean+iterations as a
        # weighted summary instead.
        all_warm_ms.extend([w["mean_ms"]] * w["iterations"])

    # §17.352 — roll per-stage means across queries for top-level gating.
    def _avg_stage(key: str) -> float:
        vals = [w["stage"].get(key, 0) for w in warm_results if w.get("stage")]
        return round(statistics.mean(vals), 2) if vals else 0

    record = {
        "schema_version": "1.1",  # §17.352 — added summary.stage.*
        "run_id": run_id,
        "timestamp": started,
        "hardware": _hardware(),
        "config": {
            "iterations_per_query": ITERATIONS,
            "top_k": TOP_K,
            "queries": [{"query": q, "domain": d} for q, d in QUERIES],
        },
        "cold": cold_results,
        "warm": warm_results,
        "summary": {
            "queries_total": len(QUERIES),
            "warm_iterations_total": ITERATIONS * len(QUERIES),
            "warm_mean_ms": round(
                statistics.mean(all_warm_ms), 1
            ) if all_warm_ms else 0,
            "warm_max_ms": round(
                max((w["max_ms"] for w in warm_results), default=0), 1
            ),
            "stage": {
                "embed_warm_mean_ms": _avg_stage("embed_mean_ms"),
                "search_parallel_warm_mean_ms": _avg_stage("search_parallel_mean_ms"),
                "rerank_warm_mean_ms": _avg_stage("rerank_mean_ms"),
                "rerank_per_pair_warm_mean_ms": _avg_stage("rerank_per_pair_mean_ms"),
            },
        },
        "total_bench_time_s": round(total_s, 2),
    }

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"[bench_rag] wrote {RESULTS_FILE} ({total_s:.1f}s)")
    print(f"[bench_rag] summary: warm_mean={record['summary']['warm_mean_ms']}ms "
          f"warm_max={record['summary']['warm_max_ms']}ms")


if __name__ == "__main__":
    asyncio.run(main())
