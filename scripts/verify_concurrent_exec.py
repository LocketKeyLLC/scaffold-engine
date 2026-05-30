#!/usr/bin/env python3
"""Multi-project concurrency verification harness.

Subcommands:
  setup    Create N seed projects sequentially through /ideate -> /ideate/confirm -> /dag.
  exec     Fire N concurrent /execute/all SSE streams; capture per-job timings + events.
  verdict  Run DB + log checks against the captured results; print a PASS/FAIL JSON report.
  all      setup -> exec -> verdict (one-shot).

Deps:
  - httpx (pip install httpx)
  - docker CLI on PATH with access to scaffold-orchestrator and scaffold-postgres
  - SCAFFOLD_API_KEY in env (matches the orchestrator's configured key)
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_JOBS_FILE = "/tmp/verify_concurrent_jobs.json"
DEFAULT_RESULTS_FILE = "/tmp/verify_concurrent_results.json"

# Two distinct, simple ideas that exercise CodeGen + LLM nodes with small
# outputs. Pick topically-disjoint pairs so the cross-pollution check has
# signal: a job's output should never contain the OTHER job's keywords.
DEFAULT_IDEAS = [
    ("sha256_cli", "sha256",
     "Write a Python CLI that reads stdin and prints the SHA-256 hex digest."),
    ("linecount_cli", "linecount",
     "Write a Python CLI that prints the line count of each file path argument."),
]

ORCHESTRATOR = "scaffold-orchestrator"
POSTGRES = "scaffold-postgres"
PG_USER = "scaffold"
PG_DB = "scaffold_engine"

# Phase-budget seconds — generous for CPU-only inference on this host.
T_IDEATE = 1800
T_CONFIRM = 3600
T_DAG = 900
T_EXECUTE_ALL = 7200
T_STATUS_POLL = 15


def _api_key() -> str:
    k = os.environ.get("SCAFFOLD_API_KEY", "").strip()
    if not k:
        sys.exit("ERROR: SCAFFOLD_API_KEY env var not set")
    return k


def _headers() -> dict[str, str]:
    return {"X-API-Key": _api_key(), "Content-Type": "application/json"}


def _psql(sql: str) -> list[list[str]]:
    res = subprocess.run(
        ["docker", "exec", "-i", POSTGRES,
         "psql", "-U", PG_USER, "-d", PG_DB,
         "-t", "-A", "-F", "\t", "-c", sql],
        check=False, capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        raise RuntimeError(f"psql failed: {res.stderr.strip()}")
    return [
        line.split("\t")
        for line in res.stdout.strip().splitlines()
        if line.strip()
    ]


def _logs_count(container: str, since_unix: float, *patterns: str) -> int:
    res = subprocess.run(
        ["docker", "logs", "--since", str(int(since_unix)), container],
        check=False, capture_output=True, text=True, timeout=60,
    )
    blob = (res.stdout + res.stderr).lower()
    return sum(blob.count(p.lower()) for p in patterns)


async def _get_job_status(
    client: httpx.AsyncClient, base_url: str, job_id: str
) -> str | None:
    r = await client.get(
        f"{base_url}/dag/{job_id}", headers=_headers(), timeout=30,
    )
    if r.status_code != 200:
        return None
    return r.json().get("job_status")


async def _wait_for_status(
    client: httpx.AsyncClient,
    base_url: str,
    job_id: str,
    targets: set[str],
    timeout_s: float,
    fail_on: set[str] | None = None,
) -> str:
    fail_on = fail_on or {"failed", "cancelled"}
    deadline = time.monotonic() + timeout_s
    last: str | None = None
    while time.monotonic() < deadline:
        status = await _get_job_status(client, base_url, job_id)
        if status != last:
            print(f"  [{job_id[:8]}] status={status}", flush=True)
            last = status
        if status in targets:
            return status
        if status and status in fail_on:
            raise RuntimeError(f"job {job_id} entered terminal status {status!r}")
        await asyncio.sleep(T_STATUS_POLL)
    raise TimeoutError(
        f"job {job_id} did not reach {targets} within {timeout_s}s (last={last!r})"
    )


async def setup_one(
    client: httpx.AsyncClient, base_url: str, slug: str, idea: str
) -> dict[str, Any]:
    print(f"[setup:{slug}] /ideate", flush=True)
    r = await client.post(
        f"{base_url}/ideate",
        json={"idea": idea},
        headers=_headers(),
        timeout=T_IDEATE,
    )
    r.raise_for_status()
    body = r.json()
    job_id = body.get("job_id") or body.get("id")
    if not job_id:
        raise RuntimeError(f"no job_id in /ideate response: {body}")
    print(f"[setup:{slug}] job_id={job_id}", flush=True)
    await _wait_for_status(
        client, base_url, job_id,
        {"awaiting_confirmation"},
        timeout_s=60,
    )

    print(f"[setup:{slug}] /ideate/confirm", flush=True)
    r = await client.post(
        f"{base_url}/ideate/confirm",
        json={"job_id": job_id},
        headers=_headers(),
        timeout=T_CONFIRM,
    )
    r.raise_for_status()
    await _wait_for_status(
        client, base_url, job_id,
        {"planning", "awaiting_execution", "executing", "running"},
        timeout_s=60,
    )

    print(f"[setup:{slug}] /dag", flush=True)
    r = await client.post(
        f"{base_url}/dag",
        json={"job_id": job_id},
        headers=_headers(),
        timeout=T_DAG,
    )
    r.raise_for_status()

    node_rows = _psql(
        f"SELECT node_key FROM dag_nodes WHERE job_id = '{job_id}' "
        f"ORDER BY execution_order;"
    )
    print(f"[setup:{slug}] DAG nodes: {[r[0] for r in node_rows]}", flush=True)

    return {"slug": slug, "idea": idea, "job_id": job_id,
            "node_keys": [r[0] for r in node_rows]}


async def cmd_setup(args: argparse.Namespace) -> None:
    ideas = list(DEFAULT_IDEAS)[:args.num]
    if args.ideas_file:
        custom = json.loads(Path(args.ideas_file).read_text())
        ideas = [(c["slug"], c.get("keyword", c["slug"]), c["idea"])
                 for c in custom][:args.num]
    if len(ideas) < args.num:
        sys.exit(f"ERROR: need {args.num} ideas, only {len(ideas)} available")

    out: dict[str, Any] = {
        "base_url": args.base_url,
        "started_unix": time.time(),
        "jobs": [],
    }
    async with httpx.AsyncClient() as client:
        for slug, keyword, idea in ideas:
            try:
                rec = await setup_one(client, args.base_url, slug, idea)
                rec["keyword"] = keyword
                out["jobs"].append(rec)
            except Exception as exc:
                print(f"[setup:{slug}] FAILED: {exc!r}", flush=True)
                raise
    out["finished_unix"] = time.time()
    Path(args.jobs_file).write_text(json.dumps(out, indent=2))
    print(f"\nSetup complete. {len(out['jobs'])} jobs written to {args.jobs_file}",
          flush=True)


async def stream_execute(
    client: httpx.AsyncClient,
    base_url: str,
    job: dict[str, Any],
    log_dir: Path,
) -> dict[str, Any]:
    job_id = job["job_id"]
    slug = job["slug"]
    log_path = log_dir / f"{slug}.sse.log"
    timings: dict[str, float | None] = {
        "request_sent": None,
        "first_event": None,
        "queued": None,
        "first_node_start": None,
        "first_node_done": None,
        "pipeline_complete": None,
        "execution_failed": None,
    }
    counts = {"node_start": 0, "node_done": 0, "node_retry": 0,
              "node_failed": 0, "error": 0, "queued": 0}
    final_event: dict[str, Any] | None = None

    t0 = time.monotonic()
    timings["request_sent"] = 0.0
    with log_path.open("w") as logf:
        try:
            async with client.stream(
                "POST", f"{base_url}/execute/all",
                json={"job_id": job_id},
                headers=_headers(),
                timeout=httpx.Timeout(None, connect=10.0),
            ) as response:
                cur_event: str | None = None
                async for raw in response.aiter_lines():
                    now = time.monotonic() - t0
                    if not raw:
                        cur_event = None
                        continue
                    if raw.startswith("event:"):
                        cur_event = raw[6:].strip()
                        continue
                    if not raw.startswith("data:"):
                        continue
                    payload_str = raw[5:].strip()
                    try:
                        payload = json.loads(payload_str) if payload_str else {}
                    except json.JSONDecodeError:
                        payload = {"_raw": payload_str}
                    evt = cur_event or payload.get("event") or "unknown"
                    logf.write(f"{now:7.2f}s  {evt:22}  {json.dumps(payload)}\n")
                    logf.flush()
                    if timings["first_event"] is None:
                        timings["first_event"] = now
                    if evt in counts:
                        counts[evt] += 1
                    if evt == "queued" and timings["queued"] is None:
                        timings["queued"] = now
                    if evt == "node_start" and timings["first_node_start"] is None:
                        timings["first_node_start"] = now
                    if evt == "node_done" and timings["first_node_done"] is None:
                        timings["first_node_done"] = now
                    if evt == "pipeline_complete":
                        timings["pipeline_complete"] = now
                        final_event = {"event": evt, "payload": payload}
                    if evt == "execution_failed":
                        timings["execution_failed"] = now
                        final_event = {"event": evt, "payload": payload}
        except httpx.RequestError as exc:
            logf.write(f"{time.monotonic() - t0:7.2f}s  request_error  "
                       f"{json.dumps({'err': repr(exc)})}\n")
    return {
        "slug": slug,
        "job_id": job_id,
        "keyword": job["keyword"],
        "node_keys": job["node_keys"],
        "timings_relative_s": timings,
        "event_counts": counts,
        "final_event": final_event,
        "log_path": str(log_path),
    }


async def cmd_exec(args: argparse.Namespace) -> None:
    jobs_file = Path(args.jobs_file)
    if not jobs_file.exists():
        sys.exit(f"ERROR: jobs file not found: {jobs_file}")
    setup_data = json.loads(jobs_file.read_text())
    jobs = setup_data["jobs"]
    base_url = setup_data.get("base_url", args.base_url)

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Logs -> {log_dir}", flush=True)

    started_unix = time.time()
    pool_errors_before = _logs_count(
        ORCHESTRATOR, started_unix - 86400,
        "queuepool", "TimeoutError", "pool limit",
    )

    print(f"Firing {len(jobs)} concurrent /execute/all streams...", flush=True)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[stream_execute(client, base_url, j, log_dir) for j in jobs],
            return_exceptions=True,
        )

    cleaned: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            cleaned.append({"error": repr(r)})
        else:
            cleaned.append(r)

    finished_unix = time.time()
    pool_errors_during = _logs_count(
        ORCHESTRATOR, started_unix, "queuepool", "timeouterror", "pool limit",
    )

    out = {
        "base_url": base_url,
        "started_unix": started_unix,
        "finished_unix": finished_unix,
        "wall_clock_s": finished_unix - started_unix,
        "pool_errors_during_window": pool_errors_during,
        "pool_errors_baseline_24h": pool_errors_before,
        "per_job": cleaned,
    }
    Path(args.results_file).write_text(json.dumps(out, indent=2))
    print(f"\nExec complete. Results -> {args.results_file}", flush=True)


def cmd_verdict(args: argparse.Namespace) -> None:
    results = json.loads(Path(args.results_file).read_text())
    per_job = results["per_job"]
    checks: dict[str, dict[str, Any]] = {}

    # A — DB pool stayed healthy during the concurrent window.
    pool_during = results.get("pool_errors_during_window", 0)
    checks["A_no_pool_exhaustion"] = {
        "pass": pool_during == 0,
        "pool_errors_during_window": pool_during,
        "baseline_prior_24h": results.get("pool_errors_baseline_24h", 0),
    }

    # B — all jobs reached pipeline_complete (the SSE terminal success).
    completed = [j for j in per_job
                 if isinstance(j, dict)
                 and j.get("final_event", {}).get("event") == "pipeline_complete"]
    checks["B_all_completed"] = {
        "pass": len(completed) == len(per_job),
        "completed": len(completed),
        "total": len(per_job),
        "failed_or_errored": [
            (j.get("slug"), j.get("final_event", {}).get("event") or "no_final_event")
            for j in per_job
            if isinstance(j, dict) and j not in completed
        ],
    }

    # B' — semaphore behavior: count queued events. With cap=1, expect N-1
    # queued events across N jobs. With cap>=N, expect 0.
    queued_total = sum(
        j.get("event_counts", {}).get("queued", 0)
        for j in per_job if isinstance(j, dict)
    )
    checks["B_semaphore_observed"] = {
        "queued_events_total": queued_total,
        "jobs_attempted": len(per_job),
        "interpretation": (
            "cap >= N (no queueing observed)" if queued_total == 0
            else f"cap < N -> {queued_total} job(s) queued; "
                 f"expected if execution_global_concurrency < {len(per_job)}"
        ),
    }

    # C — cross-pollution check: each job's outputs must contain its own
    # keyword and NOT the other job's keyword. Run via SQL substring match
    # on dag_nodes.output, lowercased.
    cross_findings: list[dict[str, Any]] = []
    keywords = {j["slug"]: j["keyword"]
                for j in per_job if isinstance(j, dict) and "slug" in j}
    for j in per_job:
        if not isinstance(j, dict) or "job_id" not in j:
            continue
        own_kw = keywords[j["slug"]].lower()
        other_kws = [k.lower() for s, k in keywords.items() if s != j["slug"]]
        rows = _psql(
            "SELECT node_key, COALESCE(length(output_text), 0), "
            f"COALESCE(LOWER(output_text), '') FROM dag_nodes "
            f"WHERE job_id = '{j['job_id']}' ORDER BY execution_order;"
        )
        own_hits = sum(1 for r in rows if len(r) >= 3 and own_kw in r[2])
        contamination = []
        for r in rows:
            if len(r) < 3:
                continue
            for k in other_kws:
                if k in r[2]:
                    contamination.append({"node_key": r[0], "foreign_keyword": k})
        cross_findings.append({
            "slug": j["slug"],
            "job_id": j["job_id"],
            "node_count": len(rows),
            "own_keyword_hits": own_hits,
            "contamination": contamination,
        })
    checks["C_no_cross_pollution"] = {
        "pass": all(not f["contamination"] for f in cross_findings),
        "per_job": cross_findings,
    }

    # D — per-job timing summary. Headline number is wall-clock per job vs
    # the parallel batch wall-clock. With true parallelism, per-job time
    # roughly equals batch time. With pure serialization, batch_time approx
    # equals sum(per_job_time).
    per_job_times = []
    for j in per_job:
        if not isinstance(j, dict):
            continue
        t = j.get("timings_relative_s", {})
        complete_t = t.get("pipeline_complete") or t.get("execution_failed")
        per_job_times.append({
            "slug": j.get("slug"),
            "first_event_s": t.get("first_event"),
            "queued_s": t.get("queued"),
            "first_node_start_s": t.get("first_node_start"),
            "complete_s": complete_t,
        })
    batch_s = results.get("wall_clock_s")
    sum_per_job = sum(
        (p["complete_s"] or 0.0) for p in per_job_times
    )
    parallelism = (sum_per_job / batch_s) if batch_s else None
    checks["D_timing_summary"] = {
        "batch_wall_clock_s": batch_s,
        "sum_per_job_complete_s": sum_per_job,
        "effective_parallelism": parallelism,
        "interpretation": (
            "n/a (no completions)" if parallelism is None or parallelism == 0
            else "fully parallel (~N)" if parallelism > 0.8 * len(per_job_times)
            else "partially parallel" if parallelism > 1.2
            else "effectively serial"
        ),
        "per_job": per_job_times,
    }

    overall_pass = all(
        checks[k].get("pass", True)
        for k in ("A_no_pool_exhaustion", "B_all_completed", "C_no_cross_pollution")
    )
    report = {
        "overall_pass": overall_pass,
        "n_jobs": len(per_job),
        "checks": checks,
    }
    print(json.dumps(report, indent=2))
    sys.exit(0 if overall_pass else 1)


async def cmd_all(args: argparse.Namespace) -> None:
    await cmd_setup(args)
    await cmd_exec(args)
    cmd_verdict(args)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--jobs-file", default=DEFAULT_JOBS_FILE)
    p.add_argument("--results-file", default=DEFAULT_RESULTS_FILE)
    p.add_argument("--log-dir", default="/tmp/verify_concurrent_logs")
    p.add_argument("--ideas-file",
                   help="Optional JSON file: list of {slug, keyword, idea} objects")
    p.add_argument("--num", type=int, default=2)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("exec")
    sub.add_parser("verdict")
    sub.add_parser("all")
    args = p.parse_args()

    if args.cmd == "setup":
        asyncio.run(cmd_setup(args))
    elif args.cmd == "exec":
        asyncio.run(cmd_exec(args))
    elif args.cmd == "verdict":
        cmd_verdict(args)
    elif args.cmd == "all":
        asyncio.run(cmd_all(args))


if __name__ == "__main__":
    main()
