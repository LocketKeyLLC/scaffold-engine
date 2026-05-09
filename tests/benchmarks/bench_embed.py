#!/usr/bin/env python3
"""
Scaffold Engine — Embedder + cache micro-benchmark
===================================================

Measures three numbers that the e2e bench can't isolate:

  - Cold embed:   first call after cache flush — captures Ollama's
                  embedder load time.
  - Warm embed:   same texts on a cleared cache (re-embed cost).
  - Cached read:  same texts when the cache is populated (no embed
                  call at all).

The cache hit-rate measurement is the load-bearing one for production:
if the L1 in-memory cache stops working (e.g. someone increases the
embed dim and old keys silently miss) the embedder bill quietly grows.
This bench surfaces that delta directly.

Output: append a JSONL record to tests/benchmarks/bench_embed_results.jsonl.

Usage:
    python tests/benchmarks/bench_embed.py
    # or:
    make bench-embed
    BENCH_TEXTS=20 python tests/benchmarks/bench_embed.py

Runs inside the orchestrator container (needs Ollama for the embedder
and Redis for the L2 cache).
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

# Default 10 fixed texts; override via BENCH_TEXTS env (uses generated
# unique strings so cache hits are guaranteed-cold for that count).
NUM_TEXTS = int(os.getenv("BENCH_TEXTS", "10"))

# Texts mix short / long / similar so dedup-by-canonicalization
# doesn't accidentally collapse them.
_FIXED_TEXTS = [
    "DAG orchestration patterns",
    "retrieval augmented generation",
    "transformer attention mechanism",
    "Postgres connection pooling strategies",
    "HNSW vector search indexing",
    "FastAPI async middleware order",
    "Milvus collection partition keys",
    "OpenWebUI pipeline lifecycle",
    "scaffold engine assist mode replan policies",
    "embedding dimension truncation MRL",
]


def _texts(n: int) -> list[str]:
    if n <= len(_FIXED_TEXTS):
        return _FIXED_TEXTS[:n]
    extra = [f"benchmark text {i} " + " ".join(["lorem"] * (i % 5 + 3))
             for i in range(n - len(_FIXED_TEXTS))]
    return _FIXED_TEXTS + extra


def _writable_results_file() -> Path:
    """Default to script dir; fall back to /tmp on read-only filesystem.
    See bench_rag._writable_results_file for the same reasoning."""
    override = os.getenv("BENCH_RESULTS_FILE")
    if override:
        return Path(override)
    here = Path(__file__).parent / "bench_embed_results.jsonl"
    try:
        with open(here, "a"):
            pass
        return here
    except OSError:
        fallback = Path("/tmp/scaffold-bench/bench_embed_results.jsonl")
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
    if not samples_ms:
        return {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0}
    s = sorted(samples_ms)
    n = len(s)
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


async def _clear_cache() -> None:
    """Wipe the in-memory L1 + flush the L2 Redis namespace this bench
    cares about. The full Redis is left alone — only embedv3:* keys
    are flushed to keep the bench from clobbering production data."""
    from app.utils.embedding_cache import get_cache
    cache = get_cache()
    cache._memory.clear()
    cache._l1_hits = 0
    cache._l2_hits = 0
    cache._misses = 0
    try:
        r = await cache._get_redis()
        # Scan + delete only the embed-cache namespace.
        async for key in r.scan_iter(match="embedv3:*", count=500):
            await r.delete(key)
    except Exception:
        # If Redis is down, fine — the bench just measures L1 vs cold.
        pass


async def _embed_one(text: str) -> tuple[float, bool]:
    """Embed a single string via ``rag_pipeline._embed_content`` — the same
    path RAG ingest uses, which checks the cache before hitting the
    embedder. Returns (latency_ms, was_cached_before_call).

    Earlier draft called ``model_router.embed`` directly; that bypassed
    the cache layer entirely (the cache lives one level up, in
    ``rag_pipeline._embed_content``), so cached_mean tracked uncached
    embed latency and "cache hit rate" was ~0. This route measures
    what production callers actually see.
    """
    from app.modules.rag_pipeline import _embed_content
    from app.utils.embedding_cache import get_cache
    cache = get_cache()

    was_cached = await cache.get(text) is not None
    t0 = time.monotonic()
    await _embed_content(text)
    elapsed_ms = (time.monotonic() - t0) * 1000
    return (elapsed_ms, was_cached)


async def _phase(label: str, texts: list[str]) -> dict:
    samples_ms: list[float] = []
    cached_count = 0
    for text in texts:
        elapsed_ms, was_cached = await _embed_one(text)
        samples_ms.append(elapsed_ms)
        if was_cached:
            cached_count += 1
    mean_ms = statistics.mean(samples_ms) if samples_ms else 0
    return {
        "phase": label,
        "samples": len(samples_ms),
        "cached_before_call": cached_count,
        "mean_ms": round(mean_ms, 1),
        "min_ms": round(min(samples_ms), 1) if samples_ms else 0,
        "max_ms": round(max(samples_ms), 1) if samples_ms else 0,
        **_percentiles(samples_ms),
    }


# ── Main ───────────────────────────────────────────────────────────────────


async def main() -> None:
    # See bench_rag.main for why init_clients is called here. Standalone
    # script doesn't run the FastAPI lifespan; without this, the
    # embedder calls fail with "Ollama client not initialized."
    from app.utils.http_clients import init_clients
    init_clients()

    run_id = f"embed_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    started = datetime.now(timezone.utc).isoformat()
    texts = _texts(NUM_TEXTS)
    print(f"[bench_embed] run_id={run_id} num_texts={len(texts)}")
    t0 = time.monotonic()

    # Phase 1: cold. Wipe cache → first embed of each text is a miss
    # that goes all the way to Ollama. Captures cold-load on the first,
    # warm-Ollama-cold-cache on the rest.
    print("[bench_embed] phase=cold (cache cleared)")
    await _clear_cache()
    cold = await _phase("cold", texts)
    print(f"  mean={cold['mean_ms']}ms p95={cold['p95_ms']}ms")

    # Phase 2: cached. Re-run the same texts. Every read should hit L1
    # (in-memory) so latency is ~0; if it isn't, something's broken.
    print("[bench_embed] phase=cached (same texts again)")
    cached = await _phase("cached", texts)
    print(f"  mean={cached['mean_ms']}ms p95={cached['p95_ms']}ms")

    # Phase 3: warm-no-cache. Wipe cache, re-embed. Ollama is already
    # warm but cache is fresh, so we measure embedder cost without the
    # cold-start tax.
    print("[bench_embed] phase=warm_no_cache (cache cleared, ollama warm)")
    await _clear_cache()
    warm = await _phase("warm_no_cache", texts)
    print(f"  mean={warm['mean_ms']}ms p95={warm['p95_ms']}ms")

    # Cache stats post-bench (for visibility into hit-rate during the
    # cached phase).
    from app.utils.embedding_cache import get_cache
    final_stats = get_cache().stats

    total_s = time.monotonic() - t0

    # Cache speedup = warm_no_cache mean / cached mean. Higher is better.
    # When cached_mean is 0 (full L1 hit, sub-millisecond reads) the ratio
    # is effectively unbounded — leave it None and let the bench_check
    # tool assert "cached read is fast" via a direct threshold on
    # cached_mean_ms instead of the speedup ratio.
    speedup = (
        round(warm["mean_ms"] / cached["mean_ms"], 1)
        if cached["mean_ms"] > 0 else None
    )

    record = {
        "schema_version": "1.0",
        "run_id": run_id,
        "timestamp": started,
        "hardware": _hardware(),
        "config": {"num_texts": len(texts)},
        "phases": [cold, cached, warm],
        "summary": {
            "cold_mean_ms": cold["mean_ms"],
            "warm_no_cache_mean_ms": warm["mean_ms"],
            "cached_mean_ms": cached["mean_ms"],
            "cache_speedup_x": speedup,
        },
        "cache_stats_final": final_stats,
        "total_bench_time_s": round(total_s, 2),
    }

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"[bench_embed] wrote {RESULTS_FILE} ({total_s:.1f}s)")
    print(f"[bench_embed] summary: cold={cold['mean_ms']}ms warm={warm['mean_ms']}ms "
          f"cached={cached['mean_ms']}ms speedup={speedup}x")


if __name__ == "__main__":
    asyncio.run(main())
