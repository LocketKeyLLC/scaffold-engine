#!/usr/bin/env python3
"""§17.1000 — is any role down to one usable model?

`model_ab.py` answers "which model is best for role X" but needs a hand-typed
candidate list, so it only ever grades the models someone remembered. This
answers the standing question instead: **for every role, how many models could
do its job right now** — and fails when the answer is "one".

That is the operational form of §17.999's finding. gemma4 looked like the only
model that could run triage; it was not, and the dependency was a missing retry.
A guard nobody runs would not have caught it, so this one is meant to be run
whenever the model catalog moves, which for Ollama Cloud is roughly weekly.

    docker exec scaffold-orchestrator python scripts/model_portability.py
    docker exec scaffold-orchestrator python scripts/model_portability.py \
        --roles model_triage --repeat 3 --min-viable 3

Exit codes: 0 = every role has enough alternatives, 1 = usage error,
2 = a role is below --min-viable (the whole point).

§17.993 — candidates are discovered from the Ollama tag list and then PROBED,
because a retired tag keeps listing: qwen3-coder-next was retired 2026-07-15 and
still returns 200 from /api/show while /api/chat 410s. Anything that does not
answer a one-token call is dropped before it can look like a failing model.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MIN_VIABLE_DEFAULT = 2


# §17.1001 — how many attempts each role's PRODUCTION surface makes.
#
# §17.1000 shipped `--draws` as a hand-typed flag "meant to be set to the retry
# count of the surface the role runs behind" — a correspondence nothing
# enforced. Set it too low and the guard under-reports viability and cries
# single-vendor; too high and it hides a real dependency. Either way it is a
# transcribed constant, which is the failure §17.994/§17.995 spent two entries
# on.
#
# Most roles need no entry: their gate dispatches through `model_router.
# tool_call`, which performs the §17.583 redraw itself (draws=3), so the gate
# already measures the retried path production gets. A role whose gate goes
# through `model_router.chat` gets NO internal redraw, so its surface's own
# constant has to be named here — and is IMPORTED, so changing the surface
# changes the guard with it. A test fails if a chat-dispatched role is missing.
_SURFACE_DRAW_SOURCES: dict[str, tuple[str, str]] = {
    "model_triage": ("app.native_chat.triage", "_TRIAGE_DRAWS"),
}


def surface_draws(role: str) -> int:
    """Attempts the production surface makes for ``role``. Never transcribed."""
    src = _SURFACE_DRAW_SOURCES.get(role)
    if src is None:
        return 1
    import importlib

    module, attr = src
    return int(getattr(importlib.import_module(module), attr))


def summarize_viability(rows: list[dict], min_viable: int) -> dict:
    """Pure: per-role trial rows -> viability verdict. Unit-testable, no I/O.

    A model is VIABLE for a role when it scored full marks on that role's gate —
    not "best", just "could serve". Ranking is model_ab.py's job; this only asks
    whether the role has somewhere to go if its incumbent disappears.
    """
    by_role: dict[str, dict[str, list[bool]]] = {}
    for r in rows:
        by_role.setdefault(r["role"], {}).setdefault(r["model"], []).append(
            bool(r["passed"]))
    out: dict[str, dict] = {}
    for role, models in by_role.items():
        viable = sorted(m for m, res in models.items() if res and all(res))
        out[role] = {
            "viable": viable,
            "tested": sorted(models),
            "count": len(viable),
            "ok": len(viable) >= min_viable,
        }
    return out


async def _live_models(base_url: str) -> list[str]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{base_url}/api/tags")
        r.raise_for_status()
        tags = [m["name"] for m in r.json().get("models", [])]
    keep: list[str] = []
    async with httpx.AsyncClient(timeout=90) as c:
        for tag in tags:
            try:
                resp = await c.post(
                    f"{base_url}/api/chat",
                    json={"model": tag, "messages": [{"role": "user", "content": "hi"}],
                          "stream": False, "options": {"num_predict": 1}})
            except Exception:
                continue
            if resp.status_code == 200:
                keep.append(tag)
            else:
                print(f"  skip {tag}: HTTP {resp.status_code} "
                      f"{'(retired tag still listing — §17.993)' if resp.status_code == 410 else ''}")
    return keep


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roles", nargs="+", default=None)
    ap.add_argument("--models", nargs="+", default=None,
                    help="skip discovery and probe these tags")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--draws", type=int, default=None,
                    help=("attempts per golden before counting it failed. Defaults, per "
                          "role, to what that role's PRODUCTION surface actually does "
                          "(§17.1001) — resolved from the code, not typed here. Pass a "
                          "number to override for a raw-model comparison."))
    ap.add_argument("--min-viable", type=int, default=MIN_VIABLE_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from app.config import settings
    from app.modules.model_role_learning import ROLE_TASKS
    from scripts.model_ab import TASKS, _load_goldens

    roles = args.roles or sorted(ROLE_TASKS)
    bad = [r for r in roles if r not in ROLE_TASKS]
    if bad:
        print(f"unknown roles: {bad}", file=sys.stderr)
        return 1

    from scripts.model_ab import TASKS as _T  # noqa: F811 — goldens for the estimate

    if args.dry_run:
        # Do NOT probe on a dry-run: discovery is 1 live call per tag, which is
        # the expensive half of a small run.
        n_gold = {r: len(_load_goldens(_T[ROLE_TASKS[r]].default_goldens)) for r in roles}
        n_models = len(args.models) if args.models else "N"
        print(f"\nroles: {len(roles)} | models: {n_models} | repeat: {args.repeat} "
              f"| min-viable: {args.min_viable}\n")
        for r in roles:
            print(f"  {r:24} gate={ROLE_TASKS[r]:18} goldens={n_gold[r]}")
        total = sum(n_gold.values()) * args.repeat
        print(f"\n(dry-run — {total} trials PER MODEL; "
              f"{total} x models total. Narrow with --roles / --models.)")
        return 0

    models = args.models
    if models is None:
        print("discovering live models (probing, because a retired tag keeps listing)…")
        models = await _live_models(settings.ollama_base_url)
    print(f"\nroles: {len(roles)} | models: {len(models)} | repeat: {args.repeat} "
          f"| draws: {args.draws or 'per-surface'} "
          f"| min-viable: {args.min_viable}\n")

    from app.utils.http_clients import init_clients, close_clients
    init_res = init_clients()
    if hasattr(init_res, "__await__"):
        await init_res
    rows: list[dict] = []
    try:
        for role in roles:
            task = TASKS[ROLE_TASKS[role]]
            goldens = _load_goldens(task.default_goldens)
            # §17.1001 — the surface decides, not the caller.
            role_draws = max(1, args.draws if args.draws else surface_draws(role))
            print(f"  {role:24} gate={ROLE_TASKS[role]:18} draws={role_draws}"
                  f"{' (surface default)' if not args.draws else ' (overridden)'}")
            for model in models:
                for _ in range(args.repeat):
                    for g in goldens:
                        # §17.1000 — a golden counts as failed only after `draws`
                        # attempts. The first cut always used one, and reported
                        # model_triage as single-sourced (viable=1) when §17.999
                        # had already shown all six candidates deliver 6/6
                        # through production — because run_triage redraws and
                        # the guard did not. A portability guard that measures
                        # something other than what the operator receives is the
                        # same defect this whole arc has been unwinding.
                        passed = False
                        for _draw in range(role_draws):
                            try:
                                resp = await task.dispatch(model, g, temperature=0.2,
                                                           max_tokens=4096)
                                passed = bool((await task.score(g, resp)).get("passed"))
                            except Exception:
                                passed = False
                            if passed:
                                break
                        rows.append({"role": role, "model": model, "passed": passed})
    finally:
        close_res = close_clients()
        if hasattr(close_res, "__await__"):
            await close_res

    summary = summarize_viability(rows, args.min_viable)
    print(f"{'role':24} {'gate':18} {'viable':>6}  models")
    print("-" * 96)
    failed = []
    for role in roles:
        s = summary.get(role, {"viable": [], "count": 0, "ok": False})
        mark = " " if s["ok"] else "!"
        print(f"{mark}{role:23} {ROLE_TASKS[role]:18} {s['count']:6}  "
              f"{', '.join(s['viable']) or '(none)'}")
        if not s["ok"]:
            failed.append(role)
    if failed:
        print(f"\nBELOW --min-viable={args.min_viable}: {', '.join(failed)}")
        print("A role with one usable model is a single-vendor dependency nobody "
              "chose. Either widen the candidate pool, or fix the surface — "
              "§17.999's triage 'capability gap' was a missing retry.")
        return 2
    print("\nevery role has an alternative.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
