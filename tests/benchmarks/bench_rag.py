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

# Fixed query set for reproducibility. Mix of short / long, single-domain
# / multi-domain so reranker batching gets exercised.
QUERIES = [
    ("DAG orchestration", "eng"),
    ("retrieval augmented generation patterns", "eng"),
    ("transformer attention", "ml"),
    ("Postgres connection pooling", "infra"),
    ("how does HNSW vector search work", "eng"),
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
    + mean. The first iteration of this run is still warm because we
    invoked `_bench_cold` first."""
    samples_ms: list[float] = []
    last_hits = 0
    last_backend = "unknown"
    for _ in range(iterations):
        elapsed_ms, hits, backend = await _run_one_query(query, domain)
        samples_ms.append(elapsed_ms)
        last_hits = hits
        last_backend = backend
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

    record = {
        "schema_version": "1.0",
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
