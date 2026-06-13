#!/usr/bin/env python3
"""§17.495 — model A/B harness for CodeGen-role candidate comparison.

Runs the same CodeGen goldens (tests/fixtures/codegen_goldens.json) through each
candidate Ollama model and scores the output with the SAME deterministic gates
the executor uses — structural goldens (ast.parse / must_define / must_not_contain)
+ the sandbox exec-smoke — plus latency/throughput from the ModelResponse. Emits a
side-by-side table + a JSONL record per (model, golden, repeat).

This is the objective-scoring counterpart to the ad-hoc §17.344/§17.346 A/Bs that
flipped roles to cloud models from code comments. Use it to decide whether a
coding-specialized model (e.g. qwen3-coder-next, kimi-k2.7-code, glm-5.1) beats the
generalist `model_coder` (currently qwen3.5:397b-cloud) on CodeGen.

Runs INSIDE the orchestrator container (needs app imports + Ollama + the sandbox):

    docker exec scaffold-orchestrator python scripts/model_ab.py --dry-run
    docker exec scaffold-orchestrator python scripts/model_ab.py \
        --models qwen3.5:397b-cloud qwen3-coder-next:latest --repeat 2

Exit codes: 0 = ran, 1 = CLI/usage error, 2 = no model produced any output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("scaffold.model_ab")

_DEFAULT_GOLDENS = Path(__file__).resolve().parent.parent / "tests/fixtures/codegen_goldens.json"


def _load_goldens(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    goldens = data.get("goldens") if isinstance(data, dict) else data
    if not goldens:
        sys.exit(f"ERROR: no goldens in {path}")
    return goldens


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


async def _run_one(model: str, golden: dict, *, system: str, temperature: float,
                   max_tokens: int) -> dict:
    """One (model, golden) trial: generate, sandbox-check, score, time it."""
    from app import model_router
    from app.sandbox.codegen_check import codegen_exec_smoke
    from app.utils import cost_tracking

    t0 = time.monotonic()
    try:
        with cost_tracking.call_kind("model_ab"):
            resp = await model_router.generate(
                golden["brief"], model=model, system=system,
                temperature=temperature, max_tokens=max_tokens,
            )
    except Exception as exc:  # unreachable model / pull needed / provider error
        return {"model": model, "golden": golden["id"], "ok": False,
                "error": str(exc)[:300], "passed": False}
    wall_s = round(time.monotonic() - t0, 2)

    # §17.495 — CRITICAL: model_router.generate ALWAYS computes a smart-fallback
    # (`fallback or _smart_fallback(...)`), so an unavailable candidate silently
    # runs on the fallback model. An A/B that counted that would be comparing the
    # fallback while labelling it the candidate. Reject any fallback / model
    # mismatch so an unavailable candidate is reported, never scored.
    resolved = resp.model or ""
    fell_back = getattr(resp, "fallback_used", False) or (
        resolved and resolved != model and model not in resolved)
    if fell_back:
        return {"model": model, "golden": golden["id"], "ok": False,
                "error": f"unavailable — fell back to {resolved or '?'}",
                "passed": False, "wall_s": wall_s, "resolved_model": resolved}

    if not resp.success or not (resp.text or "").strip():
        return {"model": model, "golden": golden["id"], "ok": False,
                "error": (resp.error or "empty response")[:300], "passed": False,
                "wall_s": wall_s}

    exec_res = await codegen_exec_smoke(resp.text)
    score = score_codegen(golden, resp.text, exec_res.verdict)
    return {
        "model": model, "golden": golden["id"], "ok": True,
        "passed": score["passed"],
        "structural_failures": score["structural_failures"],
        "exec_verdict": exec_res.verdict,
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


def _print_table(summary: dict[str, dict]) -> None:
    print("\n=== Model A/B — CodeGen goldens ===")
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


async def main() -> int:
    from app.config import settings

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=[settings.model_coder],
                    help="Candidate model tags (default: current model_coder).")
    ap.add_argument("--goldens", type=Path, default=_DEFAULT_GOLDENS)
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

    goldens = _load_goldens(args.goldens)
    if args.limit > 0:
        goldens = goldens[:args.limit]
    from app.modules.prompt_assembly import EXECUTION_SYSTEM_CODEGEN

    n = len(args.models) * len(goldens) * args.repeat
    print(f"Models: {args.models}")
    print(f"Goldens: {len(goldens)} ({', '.join(g['id'] for g in goldens)})")
    print(f"Repeat: {args.repeat}  →  {n} trials total")
    if args.dry_run:
        print("(dry-run — no calls made)")
        return 0

    from app.utils.http_clients import init_clients
    init_clients()

    rows: list[dict] = []
    for model in args.models:
        for golden in goldens:
            for i in range(args.repeat):
                r = await _run_one(
                    model, golden, system=EXECUTION_SYSTEM_CODEGEN,
                    temperature=args.temperature, max_tokens=args.max_tokens)
                r["repeat"] = i
                rows.append(r)
                tag = "ok" if r.get("ok") else f"ERR:{r.get('error','')[:60]}"
                mark = "✓" if r.get("passed") else "✗"
                print(f"  {model:<30} {golden['id']:<22} {mark} "
                      f"({tag}, {r.get('wall_s','?')}s, exec={r.get('exec_verdict','-')})")

    args.outfile.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    summary = _summarize(rows)
    _print_table(summary)
    print(f"Per-trial JSONL → {args.outfile}")

    if all(m["errors"] == m["trials"] for m in summary.values()):
        print("ERROR: no model produced any output (all trials errored).")
        return 2
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main()))
