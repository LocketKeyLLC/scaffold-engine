#!/usr/bin/env python3
"""§17.1072 — A/B the two decide backends on REAL operator turns, read-only.

Replays the last N operator turns of a session through decide_turn with
backend=router and backend=instructor (decide_turn writes nothing), and
reports per-turn actions, agreement, failures and latency. Run in a
throwaway dev container with the composed DATABASE_URL:

    python scripts/decide_ab.py --session <id> --n 20
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from sqlalchemy import text


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()
    from app.config import settings
    from app.database import async_session
    from app.modules import assist_decide
    from app.utils.http_clients import init_clients
    init_clients()
    async with async_session() as db:
        rows = (await db.execute(text("""
            SELECT id, node_key, content FROM assist_turns
             WHERE session_id = :sid AND role = 'operator' AND kind = 'message'
             ORDER BY id DESC LIMIT :n"""), {"sid": args.session, "n": args.n})).mappings().all()
    rows = list(reversed(rows))
    results = []
    for r in rows:
        row = {"turn": r["id"], "node_key": r["node_key"], "msg": (r["content"] or "")[:60].replace("\n", " ⏎ ")}
        for backend in ("router", "instructor"):
            settings.assist_decide_backend = backend
            t0 = time.monotonic()
            try:
                async with async_session() as db:
                    d = await assist_decide.decide_turn(session_id=args.session, message=r["content"],
                                                        node_key=r["node_key"], history=None, db=db)
                row[backend] = {"action": d.get("action"), "confidence": d.get("confidence"),
                                "fallback": bool(d.get("unavailable")), "backend": d.get("backend", "router"),
                                "ms": round((time.monotonic() - t0) * 1000)}
            except Exception as exc:
                row[backend] = {"action": None, "error": repr(exc)[:80], "ms": round((time.monotonic() - t0) * 1000)}
        row["agree"] = row["router"].get("action") == row["instructor"].get("action")
        results.append(row)
        print(json.dumps(row), flush=True)
    n = len(results)
    agree = sum(1 for x in results if x["agree"])
    r_fb = sum(1 for x in results if x["router"].get("fallback"))
    i_fb = sum(1 for x in results if x["instructor"].get("backend") != "instructor")
    r_ms = sorted(x["router"]["ms"] for x in results); i_ms = sorted(x["instructor"]["ms"] for x in results)
    med = lambda v: v[len(v) // 2] if v else 0
    print(f"\nSUMMARY turns={n} agree={agree} router_fallbacks={r_fb} instructor_fell_back_to_router={i_fb} "
          f"router_median_ms={med(r_ms)} instructor_median_ms={med(i_ms)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
