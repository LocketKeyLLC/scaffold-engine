"""§17.235 — sweep `rerank_doc_truncate` against the golden set.

Per §17.234 candidate A: the reranker's quadratic-in-sequence-length cost
makes `rerank_doc_truncate` the highest-leverage knob for /rag latency
after `rerank_max_candidates`. Halving the truncate value gives ~4×
per-pair speedup in theory, but the quality impact depends on whether
the matching content sits inside the first N chars or past the cutoff.

This script runs `scripts/score_retrieval.py` as a sidecar at each
truncate value the operator passes via ``--values``, captures the
report JSON + wall time, and prints a side-by-side comparison so the
operator can pick a default with empirical evidence rather than
guessing.

Design choices:
  * Sidecar-per-value (not in-process multi-config): the rerank settings
    are read at import time via Pydantic Settings — overriding them
    cleanly requires a fresh process. ``-e RERANK_DOC_TRUNCATE=N`` on
    the ``docker run`` does that with no app code change.
  * 6 GiB memory cap per §17.232. Same mount + user as the /rag score
    sidecar pattern established in §17.230 (mount repo RO + /tmp RW;
    UID 1000:1000 for .env read).
  * Skips a run if its report file already exists in ``--out-dir``
    unless ``--force`` — re-runs are mechanical; cached results survive
    across iterations.

Usage:
    python3 scripts/eval_doc_truncate.py --values 2000,1000,500,250
    python3 scripts/eval_doc_truncate.py --values 1000 --out-dir /tmp/eval
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def _run_one(truncate: int, golden_path: str, out_dir: Path, force: bool) -> dict:
    """Run score_retrieval as a sidecar with RERANK_DOC_TRUNCATE override."""
    report_path = out_dir / f"retrieval_report_truncate_{truncate}.json"
    container_report = f"/host-tmp/{report_path.name}"

    if report_path.exists() and not force:
        print(f"  [skip] existing {report_path.name} (use --force to re-run)")
        return {"truncate": truncate, "skipped": True, "wall_s": None,
                "report": json.loads(report_path.read_text())}

    repo_root = Path(__file__).resolve().parent.parent
    env_file = repo_root / ".env"

    cmd = [
        "docker", "run", "--rm",
        "--network", "ai-network",
        "--env-file", str(env_file),
        "--memory", "6g",
        "--user", "1000:1000",
        "-e", f"RERANK_DOC_TRUNCATE={truncate}",
        "-v", f"{repo_root}:/code:ro",
        "-v", f"{out_dir}:/host-tmp",
        "-w", "/code",
        "scaffold-engine:dev",
        "python3", "scripts/score_retrieval.py",
        "--golden", golden_path,
        "--output", container_report,
    ]

    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall_s = time.monotonic() - t0

    if proc.returncode != 0:
        print(f"  [fail] truncate={truncate} exit={proc.returncode}")
        print(f"    stderr tail: {proc.stderr[-400:]}")
        return {"truncate": truncate, "skipped": False, "wall_s": wall_s,
                "report": None, "error": f"exit={proc.returncode}"}

    if not report_path.exists():
        return {"truncate": truncate, "skipped": False, "wall_s": wall_s,
                "report": None, "error": "report not written"}

    return {"truncate": truncate, "skipped": False, "wall_s": wall_s,
            "report": json.loads(report_path.read_text())}


def _summarize(rows: list[dict]) -> None:
    """Print a comparison table across the sweep."""
    print()
    print("=" * 88)
    print("rerank_doc_truncate sweep — quality vs latency curve")
    print("=" * 88)
    print(f"{'truncate':>10}  {'wall_s':>8}  {'s/query':>9}  "
          f"{'cov@5':>7}  {'cov@10':>7}  {'mrr':>6}  {'exact_id':>9}")
    print("-" * 88)
    for row in rows:
        report = row.get("report")
        if report is None:
            err = row.get("error", "unknown")
            print(f"{row['truncate']:>10}  {'FAIL':>8}  {'':>9}  "
                  f"{'':>7}  {'':>7}  {'':>6}  {'':>9}  [{err}]")
            continue
        n = report["total_queries"]
        wall = row.get("wall_s")
        wall_str = f"{wall:.0f}" if wall else "cache"
        s_per_q = f"{wall/n:.1f}" if wall else "-"
        c5 = f"{report['coverage_at_5']:.1%}"
        c10 = f"{report['coverage_at_10']:.1%}"
        mrr = f"{report['mean_title_mrr']:.3f}"
        eid = f"{report['exact_id_coverage']:.1%}"
        print(f"{row['truncate']:>10}  {wall_str:>8}  {s_per_q:>9}  "
              f"{c5:>7}  {c10:>7}  {mrr:>6}  {eid:>9}")
    print("=" * 88)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--values", default="2000,1000,500",
                        help="comma-separated rerank_doc_truncate values")
    parser.add_argument("--golden", default="tests/fixtures/golden_set.json")
    parser.add_argument("--out-dir", default="/tmp/eval_doc_truncate", type=Path)
    parser.add_argument("--force", action="store_true",
                        help="re-run even if a report file already exists")
    args = parser.parse_args()

    values = [int(v.strip()) for v in args.values.split(",") if v.strip()]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"sweep: truncate ∈ {values}")
    print(f"golden: {args.golden}")
    print(f"reports dir: {args.out_dir}")

    rows = []
    for v in values:
        print(f"\n--- truncate={v} ---")
        rows.append(_run_one(v, args.golden, args.out_dir, args.force))

    _summarize(rows)

    # Persist the sweep summary alongside the per-value reports.
    summary_path = args.out_dir / "sweep_summary.json"
    summary_path.write_text(json.dumps([
        {"truncate": r["truncate"], "wall_s": r.get("wall_s"),
         "report": r.get("report"), "error": r.get("error")}
        for r in rows
    ], indent=2))
    print(f"\nsweep summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
