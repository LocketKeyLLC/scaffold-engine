#!/usr/bin/env python3
"""§17.495 / §17.557 — model A/B harness for per-role candidate comparison.

Runs goldens through each candidate Ollama model and scores the output with the
SAME deterministic gates the runtime uses, then emits a side-by-side table +
a JSONL record per (model, golden, repeat). Pluggable by ``--task``:

  • ``codegen`` (§17.495)   — generate() → structural goldens (ast.parse /
    must_define / must_not_contain) + sandbox exec-smoke. Decide whether a
    coding-specialized model beats the generalist ``model_coder`` on CodeGen.
  • ``extraction`` (§17.557) — tool_call(DISTILL_*) → did the model emit a
    parseable ``entries`` tool-call, and how many. This is the objective form
    of the §17.556 manual spike (coaxed qwen3.5 5/5 vs native kimi 1/5 on the
    distill prompt) — native-vs-coax reliability is per-prompt, so MEASURE it
    before any role/model switch.
  • ``verifier`` (§17.567) — tool_call(record_verification) on (task, output)
    goldens with a KNOWN-correct verdict → does the model's pass/fail match
    (verdict-match)? Mirrors execution_verify._verify_output (VERIFY_SYSTEM +
    VERIFY_TOOL, temp 0.0). Decides whether a candidate beats model_verifier.

The objective-scoring counterpart to ad-hoc A/Bs flipped from code comments
(§17.344/§17.346/§17.548). Runs INSIDE the orchestrator container (needs app
imports + Ollama + the sandbox):

    docker exec scaffold-orchestrator python scripts/model_ab.py --dry-run
    docker exec scaffold-orchestrator python scripts/model_ab.py \
        --task extraction --models qwen3.5:397b-cloud kimi-k2.7-code:cloud --repeat 5

Exit codes: 0 = ran, 1 = CLI/usage error, 2 = no model produced any output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("scaffold.model_ab")

_FIXTURES = Path(__file__).resolve().parent.parent / "tests/fixtures"
_DEFAULT_GOLDENS = _FIXTURES / "codegen_goldens.json"


def _load_goldens(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    goldens = data.get("goldens") if isinstance(data, dict) else data
    if not goldens:
        sys.exit(f"ERROR: no goldens in {path}")
    return goldens


# ---------------------------------------------------------------------------
# codegen task (§17.495) — generate + structural goldens + sandbox exec
# ---------------------------------------------------------------------------

def score_codegen(golden: dict, output: str, exec_verdict: str) -> dict:
    """Pure scoring: structural goldens + sandbox exec verdict → a verdict dict.

    ``passed`` = structural assertions all hold AND the sandbox did not return a
    definite runtime failure (``skip`` — e.g. an unresolved sibling import — is
    NOT counted against the model; it just means "couldn't run standalone").
    Kept import-light (caller supplies ``exec_verdict``) so it's unit-testable.
    """
    from tests._codegen_golden_checks import check_golden

    structural = check_golden(golden, output) if output else ["empty output"]
    passed = (not structural) and exec_verdict != "fail"
    return {
        "passed": passed,
        "structural_failures": structural,
        "exec_verdict": exec_verdict,
    }


async def _dispatch_codegen(model: str, golden: dict, *, temperature: float,
                            max_tokens: int) -> Any:
    from app import model_router
    from app.modules.prompt_assembly import EXECUTION_SYSTEM_CODEGEN
    return await model_router.generate(
        golden["brief"], model=model, system=EXECUTION_SYSTEM_CODEGEN,
        temperature=temperature, max_tokens=max_tokens,
    )


async def _score_codegen(golden: dict, resp: Any) -> dict:
    from app.sandbox.codegen_check import codegen_exec_smoke
    text = (resp.text or "")
    exec_res = await codegen_exec_smoke(text)
    s = score_codegen(golden, text, exec_res.verdict)
    return {"passed": s["passed"], "structural_failures": s["structural_failures"],
            "exec_verdict": exec_res.verdict, "metric": "exec", "metric_value": exec_res.verdict}


# ---------------------------------------------------------------------------
# extraction task (§17.557) — tool_call(DISTILL_*) + entries-produced
# ---------------------------------------------------------------------------

def score_extraction(args: dict | None) -> dict:
    """Pure scoring: a parsed tool-args dict → verdict. ``passed`` iff the model
    emitted a non-empty ``entries`` list of objects (native OR coaxed; the
    read_tool_args wrapper hides which). Import-light for unit-testability."""
    entries = []
    if args and isinstance(args.get("entries"), list):
        entries = [e for e in args["entries"] if isinstance(e, dict)]
    return {"passed": bool(entries), "entries": len(entries),
            "metric": "entries", "metric_value": len(entries)}


async def _dispatch_extraction(model: str, golden: dict, *, temperature: float,
                               max_tokens: int) -> Any:
    from app import model_router
    from app.modules.gt_extractor import (
        DISTILL_SYSTEM, DISTILL_PROMPT, RECORD_DISTILLED_ENTRIES_TOOL,
    )
    results_text = "\n\n".join(
        f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\n"
        f"Snippet: {r.get('content', '')}"
        for r in golden["results"]
    )
    return await model_router.tool_call(
        messages=[
            {"role": "system", "content": DISTILL_SYSTEM},
            {"role": "user",
             "content": DISTILL_PROMPT.format(topic=golden["topic"], results=results_text)},
        ],
        tools=[RECORD_DISTILLED_ENTRIES_TOOL],
        model=model, temperature=temperature, max_tokens=max_tokens,
    )


async def _score_extraction(golden: dict, resp: Any) -> dict:
    from app.utils.tool_call_args import read_tool_args
    return score_extraction(read_tool_args(resp))


# ---------------------------------------------------------------------------
# verifier task (§17.567) — record_verification verdict-match
# ---------------------------------------------------------------------------

def score_verifier(args: dict | None, expected: str) -> dict:
    """Pure scoring: a parsed record_verification tool-args dict + the golden's
    known-correct verdict → ``passed`` iff the model's verdict matches. A model
    that emits no parseable tool call (native miss / coax-fail) scores
    ``passed=False`` with verdict ``none`` — exactly how the production verifier
    fail-closes (execution_verify._run_verification). Import-light for tests."""
    if not args or "pass" not in args:
        return {"passed": False, "verdict": "none", "expected": expected,
                "metric": "verdict_match", "metric_value": "none"}
    verdict = "pass" if bool(args.get("pass")) else "fail"
    return {"passed": verdict == expected, "verdict": verdict,
            "expected": expected, "metric": "verdict_match",
            "metric_value": verdict}


async def _dispatch_verifier(model: str, golden: dict, *, temperature: float,
                             max_tokens: int) -> Any:
    # Mirror execution_verify._verify_output: same VERIFY_SYSTEM + VERIFY_TOOL,
    # same TASK/OUTPUT message shape, temperature 0.0 (prod verifier is
    # deterministic). The harness temperature arg is ignored on purpose so the
    # A/B reflects the real verifier call.
    from app import model_router
    from app.modules.execution_verify import VERIFY_SYSTEM, VERIFY_TOOL
    return await model_router.tool_call(
        messages=[
            {"role": "system", "content": VERIFY_SYSTEM},
            {"role": "user",
             "content": f"TASK: {golden['task']}\n\nOUTPUT:\n{golden['output']}"},
        ],
        tools=[VERIFY_TOOL],
        model=model, temperature=0.0, max_tokens=max_tokens,
    )


async def _score_verifier(golden: dict, resp: Any) -> dict:
    from app.utils.tool_call_args import read_tool_args
    return score_verifier(read_tool_args(resp), golden["expected"])


# ---------------------------------------------------------------------------
# task registry
# ---------------------------------------------------------------------------

@dataclass
class Task:
    name: str
    default_goldens: Path
    dispatch: Callable[..., Awaitable[Any]]
    score: Callable[[dict, Any], Awaitable[dict]]


TASKS: dict[str, Task] = {
    "codegen": Task("codegen", _FIXTURES / "codegen_goldens.json",
                    _dispatch_codegen, _score_codegen),
    "extraction": Task("extraction", _FIXTURES / "extraction_goldens.json",
                       _dispatch_extraction, _score_extraction),
    "verifier": Task("verifier", _FIXTURES / "verifier_goldens.json",
                     _dispatch_verifier, _score_verifier),
}


async def _is_available(model: str, base_url: str) -> bool:
    """§17.496 — pre-flight: is `model` pulled/registered (Ollama /api/show)?

    Only a definite 404 means unavailable; a 200 or any transient error returns
    True so we never false-skip a usable model. This avoids the 30-60s wasted
    fallback generation a not-pulled candidate would otherwise burn (the
    generate() smart-fallback masks 404s, §17.495)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{base_url.rstrip('/')}/api/show", json={"name": model})
        return r.status_code != 404
    except Exception:
        return True


async def _run_one(task: Task, model: str, golden: dict, *, temperature: float,
                   max_tokens: int) -> dict:
    """One (task, model, golden) trial: dispatch, reject fallback, score, time it."""
    from app.utils import cost_tracking

    t0 = time.monotonic()
    try:
        with cost_tracking.call_kind("model_ab"):
            resp = await task.dispatch(model, golden, temperature=temperature,
                                       max_tokens=max_tokens)
    except Exception as exc:  # unreachable model / pull needed / provider error
        return {"task": task.name, "model": model, "golden": golden["id"],
                "ok": False, "error": str(exc)[:300], "passed": False}
    wall_s = round(time.monotonic() - t0, 2)

    # §17.495 — CRITICAL: the dispatch always computes a smart-fallback, so an
    # unavailable candidate silently runs on the fallback model. An A/B that
    # counted that would compare the fallback while labelling it the candidate.
    # Reject any fallback / model mismatch so it's reported, never scored.
    resolved = resp.model or ""
    fell_back = getattr(resp, "fallback_used", False) or (
        resolved and resolved != model and model not in resolved)
    if fell_back:
        return {"task": task.name, "model": model, "golden": golden["id"],
                "ok": False, "error": f"unavailable — fell back to {resolved or '?'}",
                "passed": False, "wall_s": wall_s, "resolved_model": resolved}

    if not resp.success:
        return {"task": task.name, "model": model, "golden": golden["id"],
                "ok": False, "error": (resp.error or "no response")[:300],
                "passed": False, "wall_s": wall_s}

    verdict = await task.score(golden, resp)
    return {
        "task": task.name, "model": model, "golden": golden["id"], "ok": True,
        "passed": verdict["passed"],
        "metric": verdict.get("metric"), "metric_value": verdict.get("metric_value"),
        "structural_failures": verdict.get("structural_failures"),
        "exec_verdict": verdict.get("exec_verdict"),
        "entries": verdict.get("entries"),
        "wall_s": wall_s,
        "ttft_ms": resp.ttft_ms,
        "total_duration_ms": resp.total_duration_ms,
        "tokens_completion": resp.tokens_completion,
        "tokens_per_sec": resp.tokens_per_sec,
        "resolved_model": resp.model,
    }


def _summarize(rows: list[dict]) -> dict[str, dict]:
    """Aggregate trial rows into per-model summary."""
    out: dict[str, dict] = {}
    for r in rows:
        m = out.setdefault(r["model"], {
            "trials": 0, "passed": 0, "errors": 0,
            "wall_s": [], "tps": [], "ttft_ms": []})
        m["trials"] += 1
        if not r.get("ok"):
            m["errors"] += 1
            continue
        m["passed"] += 1 if r["passed"] else 0
        if r.get("wall_s") is not None:
            m["wall_s"].append(r["wall_s"])
        if r.get("tokens_per_sec"):
            m["tps"].append(r["tokens_per_sec"])
        if r.get("ttft_ms"):
            m["ttft_ms"].append(r["ttft_ms"])
    return out


def _avg(xs: list) -> float:
    return round(sum(xs) / len(xs), 2) if xs else 0.0


def _print_table(summary: dict[str, dict], task_name: str = "codegen") -> None:
    print(f"\n=== Model A/B — {task_name} goldens ===")
    print(f"{'model':<34} {'pass':>10} {'err':>4} {'avg_wall_s':>11} "
          f"{'avg_tps':>8} {'avg_ttft_ms':>12}")
    print("-" * 84)
    for model, m in summary.items():
        scored = m["trials"] - m["errors"]
        rate = f"{m['passed']}/{scored}" if scored else "0/0"
        print(f"{model:<34} {rate:>10} {m['errors']:>4} "
              f"{_avg(m['wall_s']):>11} {_avg(m['tps']):>8} "
              f"{_avg(m['ttft_ms']):>12}")
    print()


async def run_model_ab_task(
    task_name: str,
    models: list[str],
    *,
    repeat: int = 1,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    limit: int = 0,
) -> dict:
    """§17.578 — library API for the model A/B harness (used by the scheduled
    re-A/B governance job). Runs every (model, golden, repeat) trial for
    ``task_name`` and returns ``{"task", "models", "summary": <per-model dict>,
    "rows": <trial rows>}``. Shares _load_goldens/_is_available/_run_one/
    _summarize with the CLI ``main()``; caller is responsible for init_clients()."""
    from app.config import settings
    if task_name not in TASKS:
        raise ValueError(f"unknown task: {task_name!r} (have {sorted(TASKS)})")
    task = TASKS[task_name]
    goldens = _load_goldens(task.default_goldens)
    if limit > 0:
        goldens = goldens[:limit]

    available = {m: await _is_available(m, settings.ollama_base_url) for m in models}
    rows: list[dict] = []
    for model in models:
        if not available[model]:
            rows.append({"task": task.name, "model": model, "golden": "(preflight)",
                         "ok": False, "passed": False, "error": "not pulled"})
            continue
        for golden in goldens:
            for i in range(repeat):
                r = await _run_one(task, model, golden,
                                   temperature=temperature, max_tokens=max_tokens)
                r["repeat"] = i
                rows.append(r)
    return {"task": task.name, "models": models,
            "summary": _summarize(rows), "rows": rows}


async def main() -> int:
    from app.config import settings

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task", choices=sorted(TASKS), default="codegen",
                    help="Which role/task to A/B (default: codegen).")
    ap.add_argument("--models", nargs="+", default=None,
                    help="Candidate model tags (default: current model_coder).")
    ap.add_argument("--goldens", type=Path, default=None,
                    help="Override the task's default goldens fixture.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="Trials per (model, golden) — average over stochasticity.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Run only the first N goldens (0 = all) — for quick probes.")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--outfile", type=Path,
                    default=Path("/tmp/model_ab_results.jsonl"))
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the planned matrix and exit (no model calls).")
    args = ap.parse_args()

    task = TASKS[args.task]
    models = args.models or [settings.model_coder]
    goldens = _load_goldens(args.goldens or task.default_goldens)
    if args.limit > 0:
        goldens = goldens[:args.limit]

    n = len(models) * len(goldens) * args.repeat
    print(f"Task:    {task.name}")
    print(f"Models:  {models}")
    print(f"Goldens: {len(goldens)} ({', '.join(g['id'] for g in goldens)})")
    print(f"Repeat:  {args.repeat}  →  {n} trials total")
    if args.dry_run:
        print("(dry-run — no calls made)")
        return 0

    from app.utils.http_clients import init_clients
    init_clients()

    # §17.496 — pre-flight availability: skip not-pulled models instantly.
    available: dict[str, bool] = {}
    for model in models:
        available[model] = await _is_available(model, settings.ollama_base_url)
        if not available[model]:
            print(f"  ⚠ {model}: not pulled — skipping (run: ollama pull {model})")

    rows: list[dict] = []
    for model in models:
        if not available[model]:
            rows.append({"task": task.name, "model": model, "golden": "(preflight)",
                         "ok": False, "passed": False,
                         "error": "not pulled — ollama pull required"})
            continue
        for golden in goldens:
            for i in range(args.repeat):
                r = await _run_one(task, model, golden, temperature=args.temperature,
                                   max_tokens=args.max_tokens)
                r["repeat"] = i
                rows.append(r)
                tag = "ok" if r.get("ok") else f"ERR:{r.get('error','')[:60]}"
                mark = "✓" if r.get("passed") else "✗"
                metric = f"{r.get('metric','')}={r.get('metric_value','-')}" if r.get("ok") else ""
                print(f"  {model:<30} {golden['id']:<22} {mark} "
                      f"({tag}, {r.get('wall_s','?')}s, {metric})")

    args.outfile.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    summary = _summarize(rows)
    _print_table(summary, task.name)
    print(f"Per-trial JSONL → {args.outfile}")

    if all(m["errors"] == m["trials"] for m in summary.values()):
        print("ERROR: no model produced any output (all trials errored).")
        return 2
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main()))
