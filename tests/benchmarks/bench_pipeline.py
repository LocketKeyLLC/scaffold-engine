#!/usr/bin/env python3
"""
Scaffold Engine — Performance Benchmark Suite
==============================================
Measures and records baseline timings for the full pipeline:
  - Ollama model load state (warm/cold)
  - Raw Ollama inference (tokens/sec, TTFT, prompt eval)
  - DAG generation wall-clock time
  - Per-node execution time (via SSE event stream)
  - Total pipeline time
  - System metrics (CPU, memory) during benchmark

Results appended as JSONL to tests/benchmarks/results.jsonl

Usage:
    python tests/benchmarks/bench_pipeline.py
    # or via Makefile:
    make bench
"""

import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx required. Install with: pip install httpx")
    sys.exit(1)

try:
    import psutil
except ImportError:
    print("ERROR: psutil required. Install with: pip install psutil")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────────

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://172.18.0.1:11434")
SCAFFOLD_URL = os.getenv("SCAFFOLD_URL", "http://localhost:8000")
API_KEY = os.getenv("SCAFFOLD_API_KEY", "***REMOVED***")

# Fixed reproducible benchmark job — 3-node research task
BENCHMARK_IDEA = (
    "Research the key differences between retrieval-augmented generation "
    "and fine-tuning for domain-specific knowledge. First, look up RAG "
    "pipeline architecture patterns from our knowledge base. Then, "
    "summarize the tradeoffs between RAG and fine-tuning approaches. "
    "Finally, produce a concise recommendation for small teams with "
    "limited compute budgets."
)

# Models to benchmark for raw inference
RAW_INFERENCE_MODELS = [
    "qwen2.5:7b",       # verifier
    "qwen3:4b",          # query generation
]

# Fixed prompt for raw inference timing
RAW_INFERENCE_PROMPT = (
    "Explain the difference between retrieval-augmented generation and "
    "fine-tuning in exactly three sentences."
)

RESULTS_DIR = Path(__file__).parent
RESULTS_FILE = RESULTS_DIR / "results.jsonl"

# ── System Metrics Collector ───────────────────────────────────────────────

class MetricsCollector:
    """Background thread collecting CPU/memory at 500ms intervals."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.samples = []

    def start(self):
        # Prime the cpu_percent counters (first call always returns 0.0)
        psutil.cpu_percent(interval=None, percpu=False)
        self._thread = threading.Thread(target=self._collect, daemon=True)
        self._thread.start()

    def _collect(self):
        while not self._stop.wait(self.interval):
            mem = psutil.virtual_memory()
            self.samples.append({
                "ts": time.time(),
                "cpu_pct": psutil.cpu_percent(interval=None, percpu=False),
                "mem_used_mb": round(mem.used / 1024 / 1024),
                "mem_pct": mem.percent,
            })

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if not self.samples:
            return {"avg_cpu_pct": 0, "peak_cpu_pct": 0,
                    "avg_mem_pct": 0, "peak_mem_mb": 0, "sample_count": 0}
        cpus = [s["cpu_pct"] for s in self.samples]
        mems = [s["mem_used_mb"] for s in self.samples]
        return {
            "avg_cpu_pct": round(sum(cpus) / len(cpus), 1),
            "peak_cpu_pct": round(max(cpus), 1),
            "avg_mem_pct": round(
                sum(s["mem_pct"] for s in self.samples) / len(self.samples), 1
            ),
            "peak_mem_mb": max(mems),
            "sample_count": len(self.samples),
        }


# ── Helpers ────────────────────────────────────────────────────────────────

def timed(fn, *args, **kwargs):
    """Run fn, return (result, elapsed_seconds)."""
    t0 = time.monotonic()
    result = fn(*args, **kwargs)
    return result, round(time.monotonic() - t0, 3)


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}")


def get_hardware_info() -> dict:
    """Collect static hardware identifiers for the result record."""
    mem = psutil.virtual_memory()
    try:
        cpu_model = subprocess.check_output(
            ["grep", "-m1", "model name", "/proc/cpuinfo"],
            text=True
        ).split(":")[1].strip()
    except Exception:
        cpu_model = platform.processor() or "unknown"
    return {
        "cpu": cpu_model,
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "ram_total_mb": round(mem.total / 1024 / 1024),
        "platform": platform.platform(),
    }


# ── Ollama Probes ──────────────────────────────────────────────────────────

def ollama_ps() -> list:
    """Return list of currently loaded models via /api/ps."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/ps", timeout=10)
        r.raise_for_status()
        return r.json().get("models", [])
    except Exception as e:
        log(f"WARNING: /api/ps failed: {e}")
        return []


def ollama_warm(model: str):
    """Preload a model + warm the prompt-eval path.

    §17.355 — pre-§17.355 this sent only an empty-prompt request,
    which loads weights but doesn't exercise the prompt-eval CPU
    cache. The subsequent first real inference paid a 2-4 s cold-
    prompt-eval penalty that was misattributed across the 2026-04-02
    → 2026-05-31 bench runs as a "TTFT improvement." Reality: it
    tracked Ollama keep-alive state at preflight, not any code
    change. Fix: also send a one-token throwaway generation so
    prompt-eval CPU caches are hot before the benchmark call. The
    extra cost is ~50 ms; the payoff is that TTFT becomes a stable
    measurement of warm prompt-eval rather than first-call.
    """
    log(f"Warming model: {model}")
    try:
        # Load weights (mmap, GPU upload, etc.)
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": "30m"},
            timeout=300,
        )
        r.raise_for_status()
        # §17.355 — exercise prompt-eval + one token of decode so the
        # subsequent benchmark call doesn't pay first-call CPU-cache cost.
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model, "prompt": "1+1=",
                "stream": False, "options": {"num_predict": 1},
                "keep_alive": "30m",
            },
            timeout=60,
        )
        r.raise_for_status()
    except Exception as e:
        log(f"WARNING: warm failed for {model}: {e}")


def ollama_raw_inference(model: str, prompt: str) -> dict:
    """
    Run a single non-streaming inference and capture Ollama's
    nanosecond timing fields.
    """
    r = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 128},
        },
        timeout=600,
    )
    r.raise_for_status()
    data = r.json()

    # Convert nanoseconds → seconds for readability
    ns = 1_000_000_000
    eval_dur = data.get("eval_duration", 0)
    eval_count = data.get("eval_count", 0)
    prompt_eval_dur = data.get("prompt_eval_duration", 0)
    prompt_eval_count = data.get("prompt_eval_count", 0)

    return {
        "model": model,
        "total_duration_s": round(data.get("total_duration", 0) / ns, 3),
        "load_duration_s": round(data.get("load_duration", 0) / ns, 3),
        "prompt_eval_count": prompt_eval_count,
        "prompt_eval_duration_s": round(prompt_eval_dur / ns, 3),
        "prompt_eval_tps": (
            round(prompt_eval_count / (prompt_eval_dur / ns), 1)
            if prompt_eval_dur > 0 else 0
        ),
        "eval_count": eval_count,
        "eval_duration_s": round(eval_dur / ns, 3),
        "eval_tps": (
            round(eval_count / (eval_dur / ns), 1)
            if eval_dur > 0 else 0
        ),
        "ttft_approx_s": round(
            (data.get("load_duration", 0) + prompt_eval_dur) / ns, 3
        ),
    }


# ── Scaffold Engine API ───────────────────────────────────────────────────

def scaffold_headers() -> dict:
    return {
        "X-API-Key": API_KEY,
        "Content-Type": "application/json",
    }


def submit_idea(idea: str) -> tuple[str, float]:
    """POST /ideate → returns (job_id, elapsed_seconds).

    §17.353 — switched from the legacy `/ideas` (one-shot refine + DAG)
    to the modern `/ideate` (Phase 1: analyze and halt for confirmation).
    `idea_submission` in the bench record now exclusively measures the
    analyze-and-confirm gate, not the bundled refine+DAG of `/ideas`.
    Field name preserved so bench_check.py's
    ``pipeline.idea_submission.duration_s`` keeps gating without
    schema-aware changes downstream.
    """
    t0 = time.monotonic()
    r = httpx.post(
        f"{SCAFFOLD_URL}/ideate",
        headers=scaffold_headers(),
        json={"idea": idea},
        timeout=300,
    )
    elapsed = round(time.monotonic() - t0, 3)
    r.raise_for_status()
    data = r.json()
    job_id = data.get("job_id") or data.get("id")
    if not job_id:
        raise RuntimeError(f"No job_id in response: {data}")
    return job_id, elapsed


def confirm_idea(job_id: str) -> tuple[dict, float]:
    """POST /ideate/confirm → returns (confirm_response, elapsed_seconds).

    §17.353 — the explicit Phase-2 confirmation step that runs research
    and compiles the workflow, transitioning the job from
    ``awaiting_confirmation`` to ``planning``. Recorded under a NEW
    ``pipeline.confirmation`` field so the existing
    ``pipeline.dag_generation.duration_s`` keeps timing just DAG-gen
    (now via the explicit ``POST /dag`` call which §17.353 brings back
    after this confirm step).
    """
    t0 = time.monotonic()
    r = httpx.post(
        f"{SCAFFOLD_URL}/ideate/confirm",
        headers=scaffold_headers(),
        json={"job_id": job_id},
        timeout=600,
    )
    elapsed = round(time.monotonic() - t0, 3)
    r.raise_for_status()
    return r.json(), elapsed


def generate_dag(job_id: str) -> tuple[dict, float]:
    """POST /dag → returns (dag_response, elapsed_seconds).

    §17.353 — restored after the bench moved to the explicit
    ``/ideate`` + ``/ideate/confirm`` flow. ``/ideate/confirm``
    (research_and_compile) does not auto-generate the DAG (unlike the
    legacy ``/ideas``), so this call is the actual DAG-generation
    timer. ``pipeline.dag_generation.duration_s`` therefore measures
    DAG-gen alone — no research, no execution.

    409 retained from §17.351 as defense-in-depth: if a future code
    change re-introduces auto-DAG in confirm, the bench still works
    (records 0 with the auto marker rather than failing).
    """
    t0 = time.monotonic()
    r = httpx.post(
        f"{SCAFFOLD_URL}/dag",
        headers=scaffold_headers(),
        json={"job_id": job_id},
        timeout=600,
    )
    elapsed = round(time.monotonic() - t0, 3)
    if r.status_code == 409:
        return {"auto_generated_during_earlier_phase": True}, 0.0
    r.raise_for_status()
    return r.json(), elapsed


def execute_and_stream(job_id: str) -> tuple[list, float]:
    """
    POST /execute/all → stream SSE events.
    Returns (events_list, total_elapsed_seconds).
    Each event: {"event": name, "data": parsed_json, "wall_ts": float}
    """
    events = []
    t0 = time.monotonic()

    with httpx.stream(
        "POST",
        f"{SCAFFOLD_URL}/execute/all",
        headers=scaffold_headers(),
        json={"job_id": job_id},
        timeout=httpx.Timeout(connect=30, read=600, write=30, pool=30),
    ) as response:
        response.raise_for_status()
        buf = ""
        current_event = "message"

        for chunk in response.iter_text():
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")

                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {"raw": raw}
                    events.append({
                        "event": current_event,
                        "data": data,
                        "wall_ts": round(time.monotonic() - t0, 3),
                    })
                    current_event = "message"
                elif line == "":
                    # Empty line = end of event block (already captured above)
                    pass

    elapsed = round(time.monotonic() - t0, 3)
    return events, elapsed


def parse_node_timings(events: list) -> list:
    """
    Extract per-node wall-clock timings from SSE events.
    Looks for node_started / node_completed event pairs.
    """
    node_starts = {}
    node_timings = []

    for ev in events:
        name = ev["event"]
        data = ev["data"] if isinstance(ev["data"], dict) else {}
        wall = ev["wall_ts"]

        # Detect node start events
        if name in ("node_start"):
            node_key = data.get("node_key") or data.get("node") or data.get("key")
            if node_key:
                node_starts[node_key] = {
                    "start_ts": wall,
                    "tool": data.get("tool", "unknown"),
                }

        # Detect node completion events
        elif name in ("node_done"):
            node_key = data.get("node_key") or data.get("node") or data.get("key")
            if node_key and node_key in node_starts:
                start_info = node_starts[node_key]
                node_timings.append({
                    "node_key": node_key,
                    "tool": start_info["tool"],
                    "duration_s": round(wall - start_info["start_ts"], 3),
                })

    return node_timings


# ── Benchmark Phases ───────────────────────────────────────────────────────

def phase_ollama_raw(collector: MetricsCollector) -> list:
    """Phase 1: Raw Ollama inference benchmarks."""
    print("\n── Phase 1: Raw Ollama Inference ──")
    results = []

    for model in RAW_INFERENCE_MODELS:
        # Ensure model is loaded
        ollama_warm(model)
        time.sleep(2)  # Let it settle

        log(f"Benchmarking {model} (warm, temp=0, max_tokens=128)...")
        timing = ollama_raw_inference(model, RAW_INFERENCE_PROMPT)
        log(f"  → {timing['eval_tps']} tok/s generation, "
            f"{timing['prompt_eval_tps']} tok/s prompt eval, "
            f"TTFT ~{timing['ttft_approx_s']}s")
        results.append(timing)

    return results


def phase_pipeline(collector: MetricsCollector) -> dict:
    """Phase 2: Full Scaffold Engine pipeline benchmark.

    §17.353 — four explicit phases via the modern endpoint flow:
    ``/ideate`` (analyze + halt) → ``/ideate/confirm`` (research +
    compile) → ``/dag`` (DAG generation) → ``/execute/all`` (stream
    node execution). Replaces the pre-§17.353 ``/ideas`` + ``/dag``
    shape which bundled refinement and DAG into one timer and forced
    bench_pipeline to special-case a 409 from the auto-DAG path.

    Field-name preservation: ``idea_submission``, ``dag_generation``,
    ``execution``, ``total_pipeline_s`` all keep their pre-§17.353
    shape — only their underlying endpoint changes — so the
    bench_check.py ``pipeline.total_pipeline_s`` regression gate keeps
    firing without schema-aware downstream changes. New
    ``confirmation`` field captures the additional Phase-2 step.
    """
    print("\n── Phase 2: Full Pipeline ──")
    pipeline = {}

    # Step 1: /ideate — analyze + halt for confirmation
    log("POST /ideate — analyze and assess feasibility...")
    job_id, idea_time = submit_idea(BENCHMARK_IDEA)
    log(f"  → job_id={job_id}, took {idea_time}s")
    pipeline["idea_submission"] = {
        "job_id": job_id,
        "duration_s": idea_time,
        "endpoint": "/ideate",  # §17.353 marker
    }

    # Step 2: /ideate/confirm — research + compile workflow
    log("POST /ideate/confirm — research + compile...")
    _confirm_resp, confirm_time = confirm_idea(job_id)
    log(f"  → confirm completed in {confirm_time}s")
    pipeline["confirmation"] = {
        "duration_s": confirm_time,
        "endpoint": "/ideate/confirm",  # §17.353 — new field
    }

    # Step 3: /dag — generate DAG from refined brief
    log("POST /dag — generate DAG nodes...")
    _dag_resp, dag_time = generate_dag(job_id)
    try:
        dag_get = httpx.get(
            f"{SCAFFOLD_URL}/dag/{job_id}",
            headers=scaffold_headers(),
            timeout=30,
        )
        dag_get.raise_for_status()
        dag_nodes = dag_get.json().get("nodes", [])
        node_count = len(dag_nodes)
    except Exception:
        node_count = 0
    log(f"  → {node_count} nodes generated in {dag_time}s")
    pipeline["dag_generation"] = {
        "duration_s": dag_time,
        "node_count": node_count,
        "endpoint": "/dag",  # §17.353 marker
    }

    # Step 4: /execute/all — stream node execution
    log("POST /execute/all — streaming SSE...")
    events, exec_time = execute_and_stream(job_id)
    node_timings = parse_node_timings(events)
    event_types = list(set(ev["event"] for ev in events))
    log(f"  → {len(events)} SSE events, {len(node_timings)} nodes timed, "
        f"total {exec_time}s")
    for nt in node_timings:
        log(f"     {nt['node_key']}: {nt['duration_s']}s ({nt['tool']})")

    pipeline["execution"] = {
        "duration_s": exec_time,
        "total_events": len(events),
        "event_types": sorted(event_types),
        "node_timings": node_timings,
        "endpoint": "/execute/all",  # §17.353 marker
    }

    # Total pipeline time — sums all four §17.353 phases. Pre-§17.353
    # runs summed only three (idea + dag + exec); the new confirmation
    # phase makes total_pipeline_s slightly higher, but the gate's
    # 1.5× threshold has plenty of headroom.
    pipeline["total_pipeline_s"] = round(
        idea_time + confirm_time + dag_time + exec_time, 3
    )

    return pipeline


# ── Main ───────────────────────────────────────────────────────────────────

def run_benchmark():
    run_id = f"bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    print(f"═══ Scaffold Engine Benchmark: {run_id} ═══")
    print(f"    Ollama:   {OLLAMA_URL}")
    print(f"    Scaffold: {SCAFFOLD_URL}")

    # Pre-flight checks
    print("\n── Pre-flight ──")
    try:
        r = httpx.get(f"{SCAFFOLD_URL}/health", headers=scaffold_headers(), timeout=10)
        r.raise_for_status()
        log("Scaffold Engine: healthy")
    except Exception as e:
        log(f"FATAL: Scaffold Engine not reachable: {e}")
        sys.exit(1)

    loaded_models = ollama_ps()
    if loaded_models:
        log(f"Ollama loaded: {[m['model'] for m in loaded_models]}")
    else:
        log("Ollama: no models currently loaded (cold start)")

    hardware = get_hardware_info()
    log(f"Hardware: {hardware['cpu']}, {hardware['cores_physical']}C/"
        f"{hardware['cores_logical']}T, {hardware['ram_total_mb']}MB RAM")

    # Start system metrics collection
    collector = MetricsCollector(interval=0.5)
    collector.start()
    bench_start = time.monotonic()

    # Phase 1: Raw Ollama inference
    try:
        raw_results = phase_ollama_raw(collector)
    except Exception as e:
        log(f"Phase 1 FAILED: {e}")
        raw_results = [{"error": str(e)}]

    # Phase 2: Full pipeline
    try:
        pipeline_results = phase_pipeline(collector)
    except Exception as e:
        log(f"Phase 2 FAILED: {e}")
        pipeline_results = {"error": str(e)}

    # Stop collection
    bench_elapsed = round(time.monotonic() - bench_start, 3)
    system_metrics = collector.stop()

    # Check model state after benchmark
    post_models = ollama_ps()

    # ── Assemble result record ──
    record = {
        # §17.353 — schema 1.1: pipeline.* shape unchanged so existing
        # bench_check gates (pipeline.total_pipeline_s,
        # pipeline.idea_submission.duration_s, etc.) keep firing, but
        # each phase now records `endpoint:` so a future reader can tell
        # which orchestrator API actually ran. Pre-1.1 runs have no
        # endpoint marker — that's the discriminator for "this was the
        # legacy /ideas+/dag flow."
        "schema_version": "1.1",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hardware": hardware,
        "ollama": {
            "url": OLLAMA_URL,
            "models_pre": [m["model"] for m in loaded_models],
            "models_post": [m["model"] for m in post_models],
            # §17.355 — derived classifier: a "cold" preflight (no
            # models resident) historically meant a 2-4 s prompt-eval
            # penalty on the first benchmarked call that bench_pipeline
            # then misattributed as a per-model TTFT regression. Post-
            # §17.355 ollama_warm() exercises prompt-eval explicitly so
            # this classification is mostly archival, but having it in
            # the record lets a future analyst filter pre-§17.355 rows
            # cleanly when comparing TTFT trends.
            "keep_alive_state": "warm" if loaded_models else "cold",
        },
        "raw_inference": raw_results,
        "pipeline": pipeline_results,
        "system_metrics": system_metrics,
        "total_bench_time_s": bench_elapsed,
    }

    # ── Write JSONL ──
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")

    # ── Summary ──
    print(f"\n═══ Benchmark Complete: {run_id} ═══")
    print(f"    Total time: {bench_elapsed}s")
    print(f"    Results:    {RESULTS_FILE}")

    if isinstance(pipeline_results, dict) and "total_pipeline_s" in pipeline_results:
        # §17.353 — confirmation row is new; pre-1.1 records lack it
        # so guard with .get(...).
        conf = (pipeline_results.get("confirmation") or {}).get("duration_s")
        print(f"\n    Pipeline breakdown:")
        print(f"      Idea submission (/ideate):          {pipeline_results['idea_submission']['duration_s']}s")
        if conf is not None:
            print(f"      Confirmation (/ideate/confirm):     {conf}s")
        print(f"      DAG generation (/dag):              {pipeline_results['dag_generation']['duration_s']}s")
        print(f"      Execution (/execute/all):           {pipeline_results['execution']['duration_s']}s")
        print(f"      ───────────────────────────────────────────")
        print(f"      Total pipeline:                     {pipeline_results['total_pipeline_s']}s")

    if raw_results and isinstance(raw_results[0], dict) and "eval_tps" in raw_results[0]:
        print(f"\n    Raw inference:")
        for r in raw_results:
            print(f"      {r['model']}: {r['eval_tps']} tok/s gen, "
                  f"{r['prompt_eval_tps']} tok/s prompt, "
                  f"TTFT ~{r['ttft_approx_s']}s")

    print(f"\n    System: avg CPU {system_metrics['avg_cpu_pct']}%, "
          f"peak CPU {system_metrics['peak_cpu_pct']}%, "
          f"peak mem {system_metrics['peak_mem_mb']}MB")

    return record


if __name__ == "__main__":
    run_benchmark()
