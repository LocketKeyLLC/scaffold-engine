#!/usr/bin/env python3
"""
Compare benchmark results across runs.

Usage:
    python tests/benchmarks/bench_compare.py                    # last 2 runs
    python tests/benchmarks/bench_compare.py --last 5           # last 5 runs
    python tests/benchmarks/bench_compare.py --run-id bench_... # specific run
"""

import argparse
import json
import sys
from pathlib import Path

RESULTS_FILE = Path(__file__).parent / "results.jsonl"


def load_results(path: Path) -> list:
    if not path.exists():
        return []
    results = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return results


def extract_summary(record: dict) -> dict:
    """Pull key metrics into a flat dict for comparison."""
    s = {
        "run_id": record.get("run_id", "?"),
        "timestamp": record.get("timestamp", "?")[:19],
    }

    # Raw inference
    for r in record.get("raw_inference", []):
        if isinstance(r, dict) and "model" in r:
            tag = r["model"].replace(":", "_").replace(".", "_")
            s[f"{tag}_gen_tps"] = r.get("eval_tps", "?")
            s[f"{tag}_prompt_tps"] = r.get("prompt_eval_tps", "?")
            s[f"{tag}_ttft_s"] = r.get("ttft_approx_s", "?")

    # Pipeline
    p = record.get("pipeline", {})
    if isinstance(p, dict) and "total_pipeline_s" in p:
        s["idea_s"] = p.get("idea_submission", {}).get("duration_s", "?")
        s["dag_s"] = p.get("dag_generation", {}).get("duration_s", "?")
        s["dag_nodes"] = p.get("dag_generation", {}).get("node_count", "?")
        s["exec_s"] = p.get("execution", {}).get("duration_s", "?")
        s["pipeline_total_s"] = p.get("total_pipeline_s", "?")

        # Per-node
        for nt in p.get("execution", {}).get("node_timings", []):
            s[f"node_{nt['node_key']}_s"] = nt["duration_s"]

    # System
    sm = record.get("system_metrics", {})
    s["avg_cpu_pct"] = sm.get("avg_cpu_pct", "?")
    s["peak_cpu_pct"] = sm.get("peak_cpu_pct", "?")
    s["peak_mem_mb"] = sm.get("peak_mem_mb", "?")

    return s


def print_comparison(records: list):
    if not records:
        print("No benchmark results found.")
        return

    summaries = [extract_summary(r) for r in records]

    # Collect all keys
    all_keys = []
    for s in summaries:
        for k in s:
            if k not in all_keys:
                all_keys.append(k)

    # Print table
    col_width = max(20, max(len(str(s.get(k, ""))) for s in summaries for k in all_keys) + 2)
    label_width = 25

    header = f"{'Metric':<{label_width}}" + "".join(
        f"{'Run ' + str(i+1):>{col_width}}" for i in range(len(summaries))
    )
    print(header)
    print("─" * len(header))

    for key in all_keys:
        row = f"{key:<{label_width}}"
        for s in summaries:
            val = s.get(key, "—")
            row += f"{str(val):>{col_width}}"
        print(row)

    # Regression check (compare last two)
    if len(summaries) >= 2:
        prev, curr = summaries[-2], summaries[-1]
        print(f"\n── Regression Check (Run {len(summaries)-1} → {len(summaries)}) ──")
        regressions = []
        for key in ["pipeline_total_s", "dag_s", "exec_s"]:
            p = prev.get(key)
            c = curr.get(key)
            if isinstance(p, (int, float)) and isinstance(c, (int, float)) and p > 0:
                delta_pct = round((c - p) / p * 100, 1)
                symbol = "🔴" if delta_pct > 10 else "🟡" if delta_pct > 5 else "🟢"
                print(f"  {symbol} {key}: {p} → {c} ({delta_pct:+.1f}%)")
                if delta_pct > 10:
                    regressions.append(key)

        for model_key in [k for k in all_keys if k.endswith("_gen_tps")]:
            p = prev.get(model_key)
            c = curr.get(model_key)
            if isinstance(p, (int, float)) and isinstance(c, (int, float)) and p > 0:
                delta_pct = round((c - p) / p * 100, 1)
                # For tps, negative = regression
                symbol = "🔴" if delta_pct < -10 else "🟡" if delta_pct < -5 else "🟢"
                print(f"  {symbol} {model_key}: {p} → {c} ({delta_pct:+.1f}%)")
                if delta_pct < -10:
                    regressions.append(model_key)

        if regressions:
            print(f"\n  ⚠️  REGRESSIONS DETECTED: {', '.join(regressions)}")
        else:
            print(f"\n  ✅ No significant regressions.")


def main():
    parser = argparse.ArgumentParser(description="Compare benchmark runs")
    parser.add_argument("--last", type=int, default=2, help="Compare last N runs")
    parser.add_argument("--run-id", type=str, help="Show specific run by ID")
    parser.add_argument("--file", type=str, default=str(RESULTS_FILE),
                        help="Path to results.jsonl")
    args = parser.parse_args()

    results = load_results(Path(args.file))

    if args.run_id:
        results = [r for r in results if r.get("run_id") == args.run_id]
    else:
        results = results[-args.last:]

    print_comparison(results)


if __name__ == "__main__":
    main()
