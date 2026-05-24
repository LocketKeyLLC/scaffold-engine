"""scripts/flatten_branched_chains.py — flatten pre-§17.269 branched
version chains in Milvus.

§17.269 closes the lockless walk+upsert race that allowed two concurrent
ingests to produce two ``version=2`` rows both pointing at the same
predecessor ``A``. Pre-fix data may contain such branches. This script
detects them and re-links the branches into a linear chain ordered by
``created_at`` (oldest stays linked to ``A``; subsequent siblings become
successors of the previous sibling).

Usage:
    python scripts/flatten_branched_chains.py             # dry-run, all domains
    python scripts/flatten_branched_chains.py --domain eng
    python scripts/flatten_branched_chains.py --apply     # actually rewrite

Default is dry-run: reports branches + would-be rewrites, exits 1 if any
branches found (so CI / runbook can gate on a clean tree). ``--apply``
performs the rewrites and exits 0 on success.

Acquires §17.269's ``_predecessor_lock`` on each row's OLD predecessor
during rewrite, so a flatten cannot race with a live ingest targeting
the same predecessor. Different predecessors don't contend.

Idempotent: a re-run after ``--apply`` finds zero branches (because the
flatten already produced a linear chain).

Exit codes:
  0  success (dry-run found no branches, OR --apply completed clean)
  1  dry-run found branches (operator decides to apply or not)
  2  argument error (unknown domain, etc.)
  3  Milvus or Postgres error during scan/rewrite
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import deque
from typing import Any

from app.config import VALID_DOMAINS
from app.utils.milvus_utils import get_collection
from app.modules.rag_pipeline import _predecessor_lock

logger = logging.getLogger("scaffold.flatten_branched_chains")


_MILVUS_QUERY_LIMIT = 16_000  # Milvus default per-query cap; paginate above.
_BFS_GUARD_FACTOR = 4  # cycle guard — cap total BFS visits at N * rows_in_domain


async def _query_all_rows(collection: Any, domain: str) -> list[dict]:
    """Return every row in `domain` with the fields we need to flatten.

    Paginates if the domain has more rows than _MILVUS_QUERY_LIMIT.
    Output fields: entry_id, version, supersedes_id, created_at — plus
    the full row body for upsert (vector + all other fields), fetched
    lazily by `_fetch_full_row` when a rewrite is actually needed.

    Domain filter is the Milvus `expr` — keyed on the partition key, so
    other domains are never read.
    """
    loop = asyncio.get_running_loop()
    rows: list[dict] = []
    offset = 0
    while True:
        page = await loop.run_in_executor(
            None,
            lambda o=offset: collection.query(
                expr=f'domain == "{domain}"',
                output_fields=["entry_id", "version", "supersedes_id", "created_at"],
                limit=_MILVUS_QUERY_LIMIT,
                offset=o,
            ),
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < _MILVUS_QUERY_LIMIT:
            break
        offset += _MILVUS_QUERY_LIMIT
    return rows


async def _fetch_full_row(collection: Any, entry_id: str, domain: str) -> dict | None:
    """Re-query the full row body for upsert. Returns None if the row
    disappeared between scan and rewrite (TTL expiry, concurrent
    deletion) — caller treats that as a no-op skip."""
    loop = asyncio.get_running_loop()
    page = await loop.run_in_executor(
        None,
        lambda: collection.query(
            expr=f'entry_id == "{entry_id}" and domain == "{domain}"',
            output_fields=["*"],  # everything we need to upsert back
            limit=1,
        ),
    )
    if not page:
        return None
    return dict(page[0])


async def _apply_rewrite(
    collection: Any,
    domain: str,
    child_row: dict,
    new_supersedes: str,
    new_version: int,
) -> bool:
    """Lock the OLD predecessor, fetch the full row, upsert with new
    supersedes_id + version + updated_at. Returns True if rewrite landed.

    The lock is on the OLD predecessor (the row's current supersedes_id
    pre-rewrite). A walk from OLD looking for successors might see this
    row briefly; the lock serializes our rewrite with any such walk.
    A walk from NEW sees either pre- or post-rewrite — both consistent.
    """
    old_predecessor = child_row["supersedes_id"]
    entry_id = child_row["entry_id"]

    # Lock OLD so live ingests targeting OLD serialize with us. If OLD
    # is empty (e.g. the row was orphaned), key on a sentinel — every
    # rewrite still goes through some lock for ordering.
    lock_key = old_predecessor or f"__orphan__{entry_id}"

    loop = asyncio.get_running_loop()
    async with _predecessor_lock(lock_key):
        full = await _fetch_full_row(collection, entry_id, domain)
        if full is None:
            logger.warning(
                "flatten_skip_missing: entry_id=%s (vanished between scan and rewrite)",
                entry_id,
            )
            return False
        full["supersedes_id"] = new_supersedes
        full["version"] = new_version
        full["updated_at"] = int(time.time())
        try:
            await loop.run_in_executor(
                None, lambda r=[full]: collection.upsert(r),
            )
        except Exception as e:
            logger.error(
                "flatten_upsert_failed: entry_id=%s err=%s",
                entry_id, e,
            )
            return False
    return True


async def flatten_domain(
    collection: Any,
    domain: str,
    *,
    apply_mode: bool,
) -> tuple[int, int]:
    """Scan + flatten branches in `domain`. Returns (branches_found, rewrites).

    BFS down each root chain. Sort siblings by created_at ASC at every
    branch point; first sibling stays linked to its parent, subsequent
    siblings become successors of the previous sibling. In dry-run mode,
    reports the planned rewrites without touching Milvus.

    `rewrites` counts the number of rows where supersedes_id OR version
    would change. In apply mode, this is the count of successful upserts;
    failed upserts are logged but do not block the rest of the sweep.
    """
    rows = await _query_all_rows(collection, domain)
    if not rows:
        logger.info("flatten: domain=%s empty, nothing to do", domain)
        return (0, 0)

    by_eid: dict[str, dict] = {r["entry_id"]: r for r in rows}
    children_by_parent: dict[str, list[dict]] = {}
    for r in rows:
        pred = r.get("supersedes_id") or ""
        if pred:
            children_by_parent.setdefault(pred, []).append(r)

    roots = [r for r in rows if not (r.get("supersedes_id") or "")]
    branches_found = 0
    rewrites = 0
    bfs_visit_cap = len(rows) * _BFS_GUARD_FACTOR

    for root in roots:
        queue = deque([(root["entry_id"], int(root["version"]))])
        visited: set[str] = {root["entry_id"]}
        visit_count = 0
        while queue:
            visit_count += 1
            if visit_count > bfs_visit_cap:
                logger.error(
                    "flatten_bfs_cap: domain=%s root=%s visits exceeded %d — "
                    "possible cycle, aborting this root",
                    domain, root["entry_id"], bfs_visit_cap,
                )
                break
            parent_eid, parent_version = queue.popleft()
            siblings = sorted(
                children_by_parent.get(parent_eid, []),
                key=lambda c: (int(c.get("created_at") or 0), c["entry_id"]),
            )
            if len(siblings) >= 2:
                branches_found += 1
                logger.info(
                    "flatten_branch: domain=%s parent=%s siblings=%d (%s)",
                    domain, parent_eid, len(siblings),
                    [s["entry_id"] for s in siblings],
                )

            prev_eid = parent_eid
            prev_version = parent_version
            for child in siblings:
                target_supersedes = prev_eid
                target_version = prev_version + 1
                needs_rewrite = (
                    child.get("supersedes_id") != target_supersedes
                    or int(child.get("version", 0)) != target_version
                )
                if needs_rewrite:
                    logger.info(
                        "flatten_rewrite%s: domain=%s entry_id=%s "
                        "supersedes: %r -> %r, version: %s -> %s",
                        " [APPLY]" if apply_mode else " [DRY-RUN]",
                        domain, child["entry_id"],
                        child.get("supersedes_id"), target_supersedes,
                        child.get("version"), target_version,
                    )
                    if apply_mode:
                        ok = await _apply_rewrite(
                            collection, domain, child,
                            target_supersedes, target_version,
                        )
                        if ok:
                            rewrites += 1
                    else:
                        rewrites += 1
                # Update local model so BFS recursion sees the new state.
                child["supersedes_id"] = target_supersedes
                child["version"] = target_version

                if child["entry_id"] not in visited:
                    visited.add(child["entry_id"])
                    queue.append((child["entry_id"], target_version))

                prev_eid = child["entry_id"]
                prev_version = target_version

    return branches_found, rewrites


async def _async_main(args: argparse.Namespace) -> int:
    collection = get_collection(raise_on_missing=False)
    if collection is None:
        logger.error("flatten: Milvus collection not available — aborting")
        return 3

    target_domains: list[str]
    if args.domain:
        if args.domain not in VALID_DOMAINS:
            logger.error(
                "flatten: unknown domain %r (valid: %s)",
                args.domain, sorted(VALID_DOMAINS),
            )
            return 2
        target_domains = [args.domain]
    else:
        target_domains = sorted(VALID_DOMAINS)

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("flatten: mode=%s domains=%s", mode, target_domains)

    total_branches = 0
    total_rewrites = 0
    try:
        for domain in target_domains:
            br, rw = await flatten_domain(
                collection, domain, apply_mode=args.apply,
            )
            total_branches += br
            total_rewrites += rw
    except Exception as e:
        logger.exception("flatten_failed: %s", e)
        return 3

    logger.info(
        "flatten_summary: mode=%s domains=%d branches=%d rewrites=%d",
        mode, len(target_domains), total_branches, total_rewrites,
    )

    # Exit code policy: dry-run with branches → 1 (operator action needed);
    # apply OR clean dry-run → 0.
    if not args.apply and total_branches > 0:
        return 1
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Flatten pre-§17.269 branched version chains in Milvus.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually rewrite branched rows. Default is dry-run.",
    )
    p.add_argument(
        "--domain", type=str, default=None,
        help=f"Scope to one domain (default: all of {sorted(VALID_DOMAINS)}).",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging.",
    )
    return p


def main() -> int:
    args = _build_argparser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
