#!/usr/bin/env python3
"""
Scaffold Engine — Benchmark regression gate
============================================

Generic regression checker over any benchmark JSONL file produced by
the bench_*.py scripts. Compares the latest run's chosen metric to
the median of the previous N runs and exits non-zero on regression.

Usage:
    # Latency metric (lower is better — "up" direction means regression):
    python tests/benchmarks/bench_check.py \\
        --file tests/benchmarks/bench_rag_results.jsonl \\
        --metric summary.warm_mean_ms \\
        --threshold 1.5 \\
        --direction up

    # Throughput metric (higher is better — "down" direction means regression):
    python tests/benchmarks/bench_check.py \\
        --file tests/benchmarks/results.jsonl \\
        --metric raw_inference.0.eval_tps \\
        --threshold 0.7 \\
        --direction down

Exit codes:
    0  — no regression OR not enough history (1 prior run is fine, 0 isn't)
    1  — argument or file error
    2  — regression detected

The "median of prior runs" baseline beats "compare to last run" because
benchmark variance shouldn't trigger a false alarm just because one
prior run was an outlier. Default `--prior-runs 3` looks at the last
3 prior runs; tune higher for a more stable but slower-to-react gate.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _resolve(record: dict, dotted: str):
    """Walk a dotted/indexed path into a JSON record. Supports list indexes
    via integer segments (e.g. ``raw_inference.0.eval_tps``)."""
    cur = record
    for seg in dotted.split("."):
        if isinstance(cur, list):
            try:
                idx = int(seg)
            except ValueError:
                return None
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        else:
            return None
    return cur


def _is_regression(latest: float, baseline: float, threshold: float, direction: str) -> bool:
    """Return True if `latest` is worse than `baseline` by >= `threshold`.

    direction='up'   → latency-style metric: regression when latest > baseline*threshold
    direction='down' → throughput-style metric: regression when latest < baseline*threshold

    `threshold` is the multiplicative limit:
      up:   1.5 means "latest must not exceed 150% of baseline"
      down: 0.7 means "latest must not fall below 70% of baseline"
    """
    if baseline == 0:
        return False  # can't compute ratio; don't false-fire on a zero baseline
    ratio = latest / baseline
    if direction == "up":
        return ratio > threshold
    if direction == "down":
        return ratio < threshold
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="Benchmark regression gate")
    p.add_argument("--file", required=True, type=Path,
                   help="Path to a benchmark JSONL results file.")
    p.add_argument("--metric", required=True,
                   help="Dotted/indexed path to the numeric metric "
                        "(e.g. 'summary.warm_mean_ms', 'raw_inference.0.eval_tps').")
    p.add_argument("--threshold", required=True, type=float,
                   help="Multiplicative limit. up: ratio not allowed to exceed; "
                        "down: ratio not allowed to fall below.")
    p.add_argument("--direction", choices=("up", "down"), required=True,
                   help="up: latency-style (higher = worse). "
                        "down: throughput-style (lower = worse).")
    p.add_argument("--prior-runs", type=int, default=3,
                   help="Number of prior runs to median (default 3).")
    args = p.parse_args()

    records = _load(args.file)
    if len(records) < 2:
        print(f"[bench_check] not enough runs for {args.metric} "
              f"({len(records)} found; need at least 2). Skipping.")
        return 0

    latest = _resolve(records[-1], args.metric)
    if not isinstance(latest, (int, float)):
        print(f"[bench_check] metric {args.metric!r} not found or non-numeric "
              f"in latest run. Skipping.")
        return 0

    prior_records = records[-1 - args.prior_runs:-1]
    prior_values: list[float] = []
    for r in prior_records:
        v = _resolve(r, args.metric)
        if isinstance(v, (int, float)):
            prior_values.append(float(v))

    if not prior_values:
        print(f"[bench_check] no usable prior values for {args.metric}. Skipping.")
        return 0

    baseline = statistics.median(prior_values)
    if _is_regression(latest, baseline, args.threshold, args.direction):
        ratio = latest / baseline if baseline else float("inf")
        print(
            f"[bench_check] REGRESSION on {args.metric}: latest={latest} "
            f"baseline_median={baseline} ratio={ratio:.2f} "
            f"(direction={args.direction} threshold={args.threshold})"
        )
        return 2
    print(
        f"[bench_check] OK on {args.metric}: latest={latest} "
        f"baseline_median={baseline} (over {len(prior_values)} prior runs)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
