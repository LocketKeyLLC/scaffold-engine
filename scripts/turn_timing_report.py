#!/usr/bin/env python3
"""§17.1109 — where does an assist turn spend its time?

Aggregates ``assist_turn_runs.timings`` (written by the turn driver at
finalize) over the last N finished runs and prints, per stage: how many turns
had it, median wall ms, median model ms inside it, and the share of the total
non-model time it accounts for. Run inside the orchestrator (or any container
with DATABASE_URL):

    docker exec scaffold-orchestrator python scripts/turn_timing_report.py --last 50
    docker exec scaffold-orchestrator python scripts/turn_timing_report.py --session <sid>

Read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import defaultdict

from sqlalchemy import text


def _p50(xs: list[int]) -> int:
    return int(statistics.median(xs)) if xs else 0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=50, help="how many finished runs (newest first)")
    ap.add_argument("--session", default=None, help="restrict to one assist session id")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    from app.database import async_session

    where = "timings IS NOT NULL AND finished_at IS NOT NULL"
    params: dict = {"n": args.last}
    if args.session:
        where += " AND session_id = :sid"
        params["sid"] = args.session
    async with async_session() as db:
        rows = (await db.execute(text(
            f"SELECT id, session_id, status, created_at, timings FROM assist_turn_runs "
            f"WHERE {where} ORDER BY created_at DESC LIMIT :n"
        ), params)).mappings().all()

    if not rows:
        print("no runs with timings yet (the column fills as turns finish after §17.1109)")
        return

    totals, llm, non_llm, calls = [], [], [], []
    by_stage: dict[str, dict[str, list[int]]] = defaultdict(lambda: {"ms": [], "llm_ms": [], "non_llm_ms": [], "calls": []})
    non_llm_sum_by_stage: dict[str, int] = defaultdict(int)
    for r in rows:
        tm = r["timings"] if isinstance(r["timings"], dict) else json.loads(r["timings"])
        totals.append(tm.get("total_ms", 0)); llm.append(tm.get("llm_ms", 0))
        non_llm.append(tm.get("non_llm_ms", 0)); calls.append(tm.get("llm_calls", 0))
        for s in tm.get("stages", []):
            b = by_stage[s["label"]]
            b["ms"].append(s["ms"]); b["llm_ms"].append(s["llm_ms"])
            b["non_llm_ms"].append(s.get("non_llm_ms", max(0, s["ms"] - s["llm_ms"])))
            b["calls"].append(s["llm_calls"])
            non_llm_sum_by_stage[s["label"]] += s.get("non_llm_ms", max(0, s["ms"] - s["llm_ms"]))

    grand_non_llm = sum(non_llm_sum_by_stage.values()) or 1
    stages = sorted(by_stage.items(), key=lambda kv: -non_llm_sum_by_stage[kv[0]])
    if args.json:
        print(json.dumps({
            "runs": len(rows),
            "turn_ms_p50": _p50(totals), "llm_ms_p50": _p50(llm), "non_llm_ms_p50": _p50(non_llm),
            "llm_calls_p50": _p50(calls),
            "stages": [{
                "label": k, "turns": len(v["ms"]), "ms_p50": _p50(v["ms"]), "llm_ms_p50": _p50(v["llm_ms"]),
                "non_llm_ms_p50": _p50(v["non_llm_ms"]), "calls_p50": _p50(v["calls"]),
                "share_of_non_llm": round(100 * non_llm_sum_by_stage[k] / grand_non_llm, 1),
            } for k, v in stages],
        }, indent=2))
        return

    print(f"runs={len(rows)}  turn p50={_p50(totals)/1000:.1f}s  llm p50={_p50(llm)/1000:.1f}s  "
          f"non-llm p50={_p50(non_llm)/1000:.1f}s  llm calls p50={_p50(calls)}")
    print(f"{'stage':<48} {'turns':>5} {'ms p50':>8} {'llm p50':>8} {'nonllm':>8} {'calls':>5} {'%nonllm':>8}")
    for k, v in stages:
        print(f"{k[:48]:<48} {len(v['ms']):>5} {_p50(v['ms']):>8} {_p50(v['llm_ms']):>8} "
              f"{_p50(v['non_llm_ms']):>8} {_p50(v['calls']):>5} "
              f"{100 * non_llm_sum_by_stage[k] / grand_non_llm:>7.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
