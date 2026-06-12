"""§17.474 — one-off backfill: recompute ``jobs.compiled_output`` for jobs
whose stored deliverable predates the §17.473 dominant-leaf fix.

Background
----------
§17.473 made compile Strategy 0 drop dead-end-branch leaves (a leaf whose
upstream is fully subsumed by a ≥2×-larger dominant leaf), so a job like the
Proxmox HomeLab one no longer compiles ``T4 (Tailscale) + T10 (validate)`` —
just ``T10``. That fix only affects *new* compiles; deliverables already
stored in the DB keep the old, dead-end-polluted text. This script re-runs
the (now-fixed) compile heuristic over existing jobs and rewrites the ones
whose output actually changes.

Safety
------
- **Dry-run by default.** Pass ``--apply`` to write.
- **Deterministic + free.** Synthesis is forced OFF for the recompute, so the
  dominant-leaf *heuristic* change is the only thing applied — no LLM calls,
  no non-determinism.
- **Synthesized jobs are skipped**, not rewritten: their stored text is an
  LLM narrative built from the old leaf set, and reproducing it would need an
  LLM pass. They're reported so you can ``/confirm`` them to regenerate.
- **Only multi-leaf jobs are touched.** The dominant-leaf change can only
  affect jobs with >1 done ``is_output_node`` leaf, so single-leaf jobs are
  never recomputed (the candidate query filters them out).
- ``updated_at`` is intentionally left alone — a backfill must not masquerade
  as user activity in the jobs list (a schema trigger, if any, still governs).

Usage (run inside the orchestrator container — needs the app's Python env)::

    docker exec scaffold-orchestrator python scripts/recompile_deliverables.py
    docker exec scaffold-orchestrator python scripts/recompile_deliverables.py --apply
    docker exec scaffold-orchestrator python scripts/recompile_deliverables.py \
        --statuses completed,failed,cancelled --apply
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import text

from app.config import settings
from app.database import get_db
from app.modules.execution_compile import _compile_output, _select_dominant_leaves


# Candidate jobs: terminal-with-a-stored-deliverable AND >1 done output-leaf
# (the only shape the §17.473 dominant-leaf change can alter). The leaf
# predicate mirrors execution_compile's Strategy-0 filter exactly:
# is_output_node AND status='done' AND output_text IS NOT NULL.
_CANDIDATES_SQL = """
    SELECT j.id::text AS id, j.title, j.compiled_output,
           j.compiled_output_synthesized AS synthesized,
           COUNT(*) FILTER (
               WHERE n.is_output_node AND n.status = 'done'
                 AND n.output_text IS NOT NULL
           ) AS leaf_count
    FROM jobs j
    JOIN dag_nodes n ON n.job_id = j.id
    WHERE j.status = ANY(:statuses)
      AND j.compiled_output IS NOT NULL
    GROUP BY j.id, j.title, j.compiled_output, j.compiled_output_synthesized
    HAVING COUNT(*) FILTER (
        WHERE n.is_output_node AND n.status = 'done'
          AND n.output_text IS NOT NULL
    ) > 1
    ORDER BY j.created_at
"""

_UPDATE_SQL = (
    "UPDATE jobs "
    "SET compiled_output = :co, compiled_output_synthesized = FALSE "
    "WHERE id = :jid"
)


async def main(apply: bool, statuses: list[str], only_job: str | None = None) -> int:
    # Force pure-heuristic recompute. This script process is separate from the
    # running uvicorn worker, so mutating the settings singleton here is
    # isolated to the backfill and never touches live request handling.
    settings.compile_synthesis_enabled = False

    agen = get_db()
    db = await agen.__anext__()
    changed = unchanged = skipped_syn = 0
    try:
        rows = await db.execute(text(_CANDIDATES_SQL), {"statuses": statuses})
        jobs = rows.mappings().all()
        if only_job:
            jobs = [j for j in jobs if j["id"] == only_job]
        print(
            f"Scanning {len(jobs)} multi-leaf candidate job(s) "
            f"(statuses={','.join(statuses)}"
            f"{', job=' + only_job if only_job else ''})\n"
        )

        for j in jobs:
            jid, title = j["id"], (j["title"] or "")[:48]
            if j["synthesized"]:
                skipped_syn += 1
                print(f"  SKIP   {jid}  {title!r}  (synthesized — /confirm to regenerate)")
                continue

            # Scope to §17.473's actual effect: only rewrite when the
            # dominant-leaf rule DROPS a leaf for this job. A multi-leaf job
            # whose recompile differs for any *other* reason (older compile
            # code, truncation drift) must not be silently rewritten under
            # this backfill's banner.
            # `tool` is required: _select_dominant_leaves protects CodeGen/LLM
            # leaves (§17.473/§17.482) via n.get("tool"), so omitting it here
            # made every node look tool-less and silently bypassed the guard —
            # this gate then over-reported drops that the live _compile_output
            # (which does SELECT tool) never makes, mis-flagging LLM-vs-LLM
            # jobs (Homelab T3, AI-Research T5) as changed.
            nrows = await db.execute(
                text(
                    "SELECT node_key, status, depends_on, tool, "
                    "       COALESCE(is_output_node, FALSE) AS is_output_node, "
                    "       output_text "
                    "FROM dag_nodes WHERE job_id = :j ORDER BY execution_order"
                ),
                {"j": jid},
            )
            nodes = [dict(r) for r in nrows.mappings().all()]
            explicit = [
                n for n in nodes
                if n["is_output_node"] and n["status"] == "done" and n["output_text"]
            ]
            dropped_keys: list[str] = []
            if len(explicit) > 1:
                _surv, dropped_keys = _select_dominant_leaves(explicit, nodes)
            if not dropped_keys:
                unchanged += 1
                continue

            new_text, _was_syn = await _compile_output(jid, db)
            old = j["compiled_output"] or ""
            new = new_text or ""
            if new == old:
                unchanged += 1
                continue

            changed += 1
            verb = "APPLY " if apply else "DRY   "
            print(
                f"  {verb} {jid}  {title!r}  "
                f"{len(old)} -> {len(new)} chars  (dropped={','.join(dropped_keys)})"
            )
            if apply:
                await db.execute(text(_UPDATE_SQL), {"co": new_text, "jid": jid})

        if apply and changed:
            await db.commit()

        print(
            f"\n{changed} changed, {unchanged} unchanged, "
            f"{skipped_syn} synthesized-skipped."
        )
        if not apply and changed:
            print("DRY RUN — no mutation. Re-run with --apply to write.")
        elif apply and changed:
            print("APPLIED — compiled_output rewritten for the changed jobs.")
    finally:
        await agen.aclose()
    return changed


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Backfill compiled_output for the §17.473 dominant-leaf fix",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="actually rewrite compiled_output (default: dry-run)",
    )
    ap.add_argument(
        "--statuses", default="completed",
        help="comma-separated job statuses to scan (default: completed)",
    )
    ap.add_argument(
        "--job", default="",
        help="restrict to a single job id (still subject to all guards)",
    )
    args = ap.parse_args()
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    asyncio.run(main(args.apply, statuses, args.job.strip() or None))
