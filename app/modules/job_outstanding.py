"""§17.1216 — one answer to "is this finished, and if not, what now".

Every surface computed that for itself, and they disagreed. On the operator's
job at the same moment:

    job status        assisted_executing
    assist progress   131/131 steps · 100%
    assist banner     "🎉 Job complete — every step is done"
    assist footer     "Nothing needed from you right now"
    dag_nodes         62 done · 53 skipped · 14 pending · 3 FAILED

The count that produced "131/131" is `committed + skipped + handed_off`, and
`handed_off` means *the autonomous executor owns this now* — not that it
succeeded. §17.1208 fixed exactly this arithmetic for the JOB STATUS and left
three siblings computing it the same wrong way: the server's
`_assist_step_progress`, the SPA's `doneN`, and the completion card that fires
off it. The operator found all three, and said so:

    "why do i keep having to reiterating the poor web ui layout to best reflect
     what the model needs so that the user can answer and assist in completion"

Because each surface had its own arithmetic. So there is one function now, it
reads `dag_nodes` — the table that records the WORK — and it answers the two
questions a surface actually needs: is this finished, and what is the next move.

The second half matters as much as the first. A page that says "3 failed" and
stops has informed nobody: on this job all 14 pending steps were waiting on
those 3 failures, `runnable` was empty, and no surface offered a move. So
`blockers` carries the reason, and `unblocks` says how many steps each failure
is holding up — which is the difference between a status line and a next step.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

logger = logging.getLogger("scaffold")

#: Work that is FINISHED. `failed` is not here, and neither is anything that
#: merely changed owner — the whole point of this module.
DONE_STATUSES = ("done", "skipped")

MAX_BLOCKERS = 6


async def outstanding(db, job_id: str) -> dict:
    """``{finished, counts, blockers, ready, total}`` for one job.

    ``finished`` is true only when nothing is pending and nothing failed.
    ``blockers`` are the failures, each with the reason and how many steps it
    holds up. ``ready`` are the pending steps that could start right now.
    Never raises: a surface that cannot read this must still render.
    """
    try:
        rows = (await db.execute(text(
            "SELECT node_key, title, status, depends_on, last_verification_reason "
            "  FROM dag_nodes WHERE job_id = :j"), {"j": str(job_id)})).mappings().all()
    except Exception as exc:
        logger.warning("outstanding_read_failed job=%s err=%r", job_id, exc)
        return {"finished": False, "known": False, "counts": {}, "blockers": [],
                "ready": [], "total": 0}
    return summarize([dict(r) for r in rows])


def summarize(nodes: list[dict]) -> dict:
    """The pure half, so it can be tested without a database."""
    by_key = {n["node_key"]: n for n in nodes}
    status = {k: (n.get("status") or "") for k, n in by_key.items()}
    counts: dict[str, int] = {}
    for s in status.values():
        counts[s] = counts.get(s, 0) + 1

    pending = [k for k, s in status.items() if s == "pending"]
    failed = [k for k, s in status.items() if s == "failed"]
    ready = [k for k in pending
             if all(status.get(d) in DONE_STATUSES for d in (by_key[k].get("depends_on") or []))]

    # How much each failure is holding up — a failure blocking eleven steps is a
    # different sentence from one blocking none.
    blockers = []
    for k in failed[:MAX_BLOCKERS]:
        blockers.append({
            "node_key": k,
            "title": (by_key[k].get("title") or "")[:120],
            "reason": (by_key[k].get("last_verification_reason") or "")[:400],
            "unblocks": len(_downstream(by_key, k) & set(pending)),
        })
    blockers.sort(key=lambda b: -b["unblocks"])

    # §17.1217 — WHAT IT IS ON NOW. The assist session's `current_node_key` is
    # session state: it freezes wherever the walkthrough stopped, and once the
    # steps are handed to the executor it points at a step nobody is working on.
    # Live, the console showed ADD65 (failed, handed off) as the current step
    # while the actual ready work was ADD50 and ADD82 — "if we are on add 50,
    # why is it on ADD 65 on the web ui? shouldn't it be showing the user what
    # its doing?" The DAG knows; the cursor does not.
    nxt = None
    if ready:
        k = sorted(ready)[0]
        nxt = {"node_key": k, "title": (by_key[k].get("title") or "")[:120]}
    elif blockers:
        b = blockers[0]
        nxt = {"node_key": b["node_key"], "title": b["title"], "blocked": True}

    return {
        "finished": not pending and not failed,
        "known": True,
        "next": nxt,
        "total": len(nodes),
        "counts": counts,
        "done": counts.get("done", 0) + counts.get("skipped", 0),
        "pending": len(pending),
        "failed": len(failed),
        "ready": sorted(ready),
        "blockers": blockers,
    }


def _downstream(by_key: dict[str, dict], start: str) -> set[str]:
    """Everything that transitively depends on `start`."""
    kids: dict[str, list[str]] = {}
    for k, n in by_key.items():
        for d in (n.get("depends_on") or []):
            kids.setdefault(d, []).append(k)
    seen: set[str] = set()
    stack = list(kids.get(start, []))
    while stack:
        k = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        stack.extend(kids.get(k, []))
    return seen


def headline(out: dict) -> str:
    """One sentence a surface can show without inventing its own arithmetic.

    Says what is true AND what happens next, because "3 failed" on its own
    leaves the operator exactly where they were.
    """
    if not out.get("known"):
        return ""
    if out.get("finished"):
        return "Every step is done or skipped."
    ready, failed, pending = out.get("ready") or [], out.get("failed") or 0, out.get("pending") or 0
    if ready:
        n = len(ready)
        return (f"{n} step{'' if n == 1 else 's'} can run now"
                + (f", and {failed} failed earlier." if failed else "."))
    if failed and pending:
        b = (out.get("blockers") or [{}])[0]
        who = f"{b.get('node_key')} " if b.get("node_key") else ""
        return (f"Stopped. {failed} step{'' if failed == 1 else 's'} failed and the remaining "
                f"{pending} are waiting on {'it' if failed == 1 else 'them'} — "
                f"{who}is holding up the most.".replace("  ", " "))
    if failed:
        return f"{failed} step{'' if failed == 1 else 's'} failed; nothing else is waiting."
    return f"{pending} step{'' if pending == 1 else 's'} left, none of them ready yet."
