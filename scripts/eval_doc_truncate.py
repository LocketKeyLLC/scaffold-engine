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


_KNOB_ENV = {
    "doc_truncate": "RERANK_DOC_TRUNCATE",
    "max_candidates": "RERANK_MAX_CANDIDATES",
}

# §17.239 — named volume holding the CrossEncoder cache. The default
# matches what `docker compose` produces from this repo
# (COMPOSE_PROJECT_NAME=scaffold-engine → `<project>_hf-cache`).
# Operators who have renamed the compose project should update this.
_HF_CACHE_VOLUME = "scaffold-engine_hf-cache"


def _cell_filename(point: list[tuple[str, int]]) -> str:
    """File name for a (knob,value) cell — supports both 1-D and N-D."""
    parts = "_".join(f"{k}_{v}" for k, v in point)
    return f"retrieval_report_{parts}.json"


def _run_cell(
    point: list[tuple[str, int]],
    golden_path: str,
    out_dir: Path,
    force: bool,
) -> dict:
    """Run score_retrieval as a sidecar with one or more env overrides.

    §17.238 — generalized from single-knob `_run_one` to take a list of
    (knob, value) tuples. For 1-D sweep the list has one element; for
    matrix sweep it has one per axis. All knobs get their own `-e` arg
    on the same `docker run`; output file name encodes the full point
    so cells don't collide on disk.
    """
    report_path = out_dir / _cell_filename(point)
    container_report = f"/host-tmp/{report_path.name}"

    if report_path.exists() and not force:
        print(f"  [skip] existing {report_path.name} (use --force to re-run)")
        return {"point": point, "skipped": True, "wall_s": None,
                "report": json.loads(report_path.read_text())}

    repo_root = Path(__file__).resolve().parent.parent
    env_file = repo_root / ".env"

    cmd = [
        "docker", "run", "--rm",
        "--network", "ai-network",
        "--env-file", str(env_file),
        "--memory", "6g",
        "--user", "1000:1000",
        # §17.239 — share the orchestrator's HF cache + go offline.
        #
        # The orchestrator container has HF_HOME=/code/.cache/huggingface
        # (baked into the image) and a named volume
        # scaffold-engine_hf-cache mounted at that path. The
        # CrossEncoder model files live there.
        #
        # Sidecars need to (a) mount the same volume so they see the
        # cache, (b) override HF_HOME to point at the sidecar's mount
        # location (we can't nest the mount under the read-only /code
        # bind), and (c) set HF_HUB_OFFLINE=1 so sentence-transformers
        # skips the rate-limited online probe — without OFFLINE=1
        # every sidecar makes an unauthenticated HF Hub round-trip
        # even when the model is cached, and after ~5 rapid sidecars
        # HF starts 429-ing the requests and the model load hangs
        # indefinitely (the §17.238 stall).
        #
        # /sidecar-hf is an arbitrary path NOT under /code; HF_HOME
        # redirection makes sentence-transformers look there for the
        # cache directory.
        #
        # Volume name is hardcoded as the default compose project
        # would produce; if you override COMPOSE_PROJECT_NAME the
        # mount target changes. Update _HF_CACHE_VOLUME at the top
        # of this module if you've renamed the compose project.
        "-e", "HF_HUB_OFFLINE=1",
        "-e", "HF_HOME=/sidecar-hf",
    ]
    for knob, value in point:
        cmd += ["-e", f"{_KNOB_ENV[knob]}={value}"]
    cmd += [
        "-v", f"{repo_root}:/code:ro",
        "-v", f"{out_dir}:/host-tmp",
        "-v", f"{_HF_CACHE_VOLUME}:/sidecar-hf:ro",
        "-w", "/code",
        "scaffold-engine:dev",
        "python3", "scripts/score_retrieval.py",
        "--golden", golden_path,
        "--output", container_report,
    ]

    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall_s = time.monotonic() - t0

    point_str = " ".join(f"{k}={v}" for k, v in point)
    if proc.returncode != 0:
        print(f"  [fail] {point_str} exit={proc.returncode}")
        print(f"    stderr tail: {proc.stderr[-400:]}")
        return {"point": point, "skipped": False, "wall_s": wall_s,
                "report": None, "error": f"exit={proc.returncode}"}

    if not report_path.exists():
        return {"point": point, "skipped": False, "wall_s": wall_s,
                "report": None, "error": "report not written"}

    return {"point": point, "skipped": False, "wall_s": wall_s,
            "report": json.loads(report_path.read_text())}


# Back-compat shim so callers of the old name still work.
def _run_one(knob: str, value: int, golden_path: str, out_dir: Path, force: bool) -> dict:
    row = _run_cell([(knob, value)], golden_path, out_dir, force)
    # Re-shape to the pre-§17.238 dict so any external caller / cached
    # test expectations keep working.
    row["knob"] = knob
    row["value"] = value
    return row


def _summarize(knob: str, rows: list[dict]) -> None:
    """Print a 1-D sweep comparison table."""
    print()
    print("=" * 88)
    print(f"rerank_{knob} sweep — quality vs latency curve")
    print("=" * 88)
    print(f"{knob:>14}  {'wall_s':>8}  {'s/query':>9}  "
          f"{'cov@5':>7}  {'cov@10':>7}  {'mrr':>6}  {'exact_id':>9}")
    print("-" * 88)
    for row in rows:
        report = row.get("report")
        if report is None:
            err = row.get("error", "unknown")
            print(f"{row['value']:>14}  {'FAIL':>8}  {'':>9}  "
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
        print(f"{row['value']:>14}  {wall_str:>8}  {s_per_q:>9}  "
              f"{c5:>7}  {c10:>7}  {mrr:>6}  {eid:>9}")
    print("=" * 88)


def _summarize_matrix(axes: list[tuple[str, list[int]]], rows: list[dict]) -> None:
    """§17.238 — print a 2-D matrix table (rows × cols).

    Renders three stacked sub-tables (coverage@5, s/query, mean_mrr) so
    the latency vs quality trade-off is visible at a glance. Only
    supports the 2-D case; 1-D falls back to ``_summarize``.
    """
    if len(axes) != 2:
        raise ValueError("_summarize_matrix requires exactly 2 axes")
    (row_knob, row_values), (col_knob, col_values) = axes
    # Index rows by point for quick lookup.
    by_point = {tuple(r["point"]): r for r in rows}

    def _fmt_cell(report: dict | None, wall_s: float | None, kind: str) -> str:
        if report is None:
            return "FAIL".rjust(8)
        if kind == "cov5":
            return f"{report['coverage_at_5']:.1%}".rjust(8)
        if kind == "cov10":
            return f"{report['coverage_at_10']:.1%}".rjust(8)
        if kind == "mrr":
            return f"{report['mean_title_mrr']:.3f}".rjust(8)
        if kind == "wall":
            if wall_s is None:
                return "cache".rjust(8)
            n = report["total_queries"]
            return f"{wall_s/n:.1f}".rjust(8) + " "
        return "?".rjust(8)

    def _print_panel(kind: str, label: str) -> None:
        print()
        print(f"--- {label}  (rows: {row_knob}, cols: {col_knob}) ---")
        header = f"{row_knob:>14}|" + "".join(f"{cv:>8}|" for cv in col_values)
        print(header)
        print("-" * len(header))
        for rv in row_values:
            line = f"{rv:>14}|"
            for cv in col_values:
                pt = ((row_knob, rv), (col_knob, cv))
                r = by_point.get(pt)
                if r is None:
                    line += "n/a".rjust(8) + "|"
                else:
                    cell = _fmt_cell(r.get("report"), r.get("wall_s"), kind)
                    line += cell + "|"
            print(line)

    print()
    print("=" * 88)
    print(f"2-D matrix sweep — {row_knob} × {col_knob}")
    print("=" * 88)
    _print_panel("cov5",  "Coverage @ top-5")
    _print_panel("cov10", "Coverage @ top-10")
    _print_panel("mrr",   "Mean title MRR")
    _print_panel("wall",  "Latency (s/query)")
    print()
    print("=" * 88)


def _parse_matrix(spec: str) -> list[tuple[str, list[int]]]:
    """Parse a `--matrix` arg.

    Format: ``knob1=v1,v2,v3`` (1-D) or
    ``knob1=v1,v2,v3:knob2=v4,v5,v6`` (2-D). Returns ordered list of
    ``(knob, [values...])`` tuples. Validates knob names against
    ``_KNOB_ENV`` and rejects unknowns with a clear error.
    """
    axes: list[tuple[str, list[int]]] = []
    for axis_spec in spec.split(":"):
        if "=" not in axis_spec:
            raise ValueError(f"axis spec missing '=': {axis_spec!r}")
        knob, vals = axis_spec.split("=", 1)
        knob = knob.strip()
        if knob not in _KNOB_ENV:
            raise ValueError(
                f"unknown knob {knob!r}; valid: {sorted(_KNOB_ENV.keys())}"
            )
        values = [int(v.strip()) for v in vals.split(",") if v.strip()]
        if not values:
            raise ValueError(f"axis {knob!r} has no values")
        axes.append((knob, values))
    if len(axes) > 2:
        raise ValueError(f"--matrix supports at most 2 axes; got {len(axes)}")
    return axes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knob", default="doc_truncate",
                        choices=sorted(_KNOB_ENV.keys()),
                        help="(1-D mode) which reranker setting to sweep")
    parser.add_argument("--values", default="2000,1000,500",
                        help="(1-D mode) comma-separated values")
    parser.add_argument(
        "--matrix", default=None,
        help="§17.238 — sweep one or two axes via a single spec, "
             "e.g. 'max_candidates=5,10:doc_truncate=250,500,1000'. "
             "Cartesian product of all axes; supersedes --knob/--values "
             "when set."
    )
    parser.add_argument("--golden", default="tests/fixtures/golden_set.json")
    parser.add_argument("--out-dir", default="/tmp/eval_doc_truncate", type=Path)
    parser.add_argument("--force", action="store_true",
                        help="re-run even if a report file already exists")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.matrix:
        axes = _parse_matrix(args.matrix)
        print(f"matrix sweep ({len(axes)}-D): {' × '.join(f'{k}∈{v}' for k,v in axes)}")
    else:
        values = [int(v.strip()) for v in args.values.split(",") if v.strip()]
        axes = [(args.knob, values)]
        print(f"sweep: rerank_{args.knob} ∈ {values}")

    print(f"golden: {args.golden}")
    print(f"reports dir: {args.out_dir}")

    # Cartesian product over the axes.
    from itertools import product
    points = [list(zip([k for k, _ in axes], combo))
              for combo in product(*[vs for _, vs in axes])]

    rows = []
    for point in points:
        print(f"\n--- {' '.join(f'{k}={v}' for k, v in point)} ---")
        rows.append(_run_cell(point, args.golden, args.out_dir, args.force))

    # Summary: 2-D matrix panels if there are 2 axes; otherwise the
    # familiar 1-D table.
    if len(axes) == 2:
        _summarize_matrix(axes, rows)
        summary_name = f"sweep_summary_{axes[0][0]}_x_{axes[1][0]}.json"
    else:
        # 1-D back-compat — rebuild old row shape (knob/value at top level)
        # so _summarize prints unchanged.
        for r in rows:
            r["knob"] = r["point"][0][0]
            r["value"] = r["point"][0][1]
        _summarize(axes[0][0], rows)
        summary_name = f"sweep_summary_{axes[0][0]}.json"

    summary_path = args.out_dir / summary_name
    summary_path.write_text(json.dumps([
        {"point": r["point"], "wall_s": r.get("wall_s"),
         "report": r.get("report"), "error": r.get("error")}
        for r in rows
    ], indent=2))
    print(f"\nsweep summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
