"""Re-embed every entry in the toon_v2 Milvus collection (Sprint, Item 7).

The 512-dim collection geometry is locked at the schema level, so swapping
``MODEL_EMBEDDER_PIPELINE`` (or its provider) without re-embedding yields
incoherent cosine similarities for the entire historical corpus. This
script does the re-embed in place.

Usage (run inside the orchestrator container — needs Milvus + Redis +
Postgres reachable on the bridge network)::

    docker exec -it scaffold-orchestrator python scripts/reindex.py [flags]

    --new-embedder <model>      Tag of the new embedder (default: keep current).
    --new-provider <ollama|openai>
                                Provider for the embedder role
                                (default: keep current).
    --domain <eng|llm|rag|spec|prompt>
                                Restrict to one Milvus partition.
                                Default: fan out across every domain.
    --batch-size <n>            Embeddings per provider call (default 32).
    --dry-run                   Count entries that would be re-embedded.
    --yes                       Skip the destructive-operation prompt.

The script preserves every entry field except ``dense_vector``, ``model_id``,
and ``updated_at``. Entries are upserted by primary key — concurrent writes
to the collection during reindex are safe (your re-embed wins for entries
you've already touched; their ingest replaces yours for entries you haven't
yet reached).

Operator workflow when migrating to a new embedder
--------------------------------------------------

1. ``--dry-run`` first to see how many entries you'll touch.
2. Quiesce ingest (stop ``/research`` jobs) — not strictly required but
   reduces the amount of new "old-geometry" data to re-touch.
3. Run reindex. Watch the progress counter.
4. Update ``MODEL_EMBEDDER_PIPELINE`` (and ``MODEL_EMBEDDER_PIPELINE_PROVIDER``)
   in ``.env``.
5. ``make restart`` to pick up the new env.
6. ``make doctor`` to confirm everything still pings.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from typing import Any

# These imports require the orchestrator's PYTHONPATH (/code). The
# Makefile target arranges that via ``docker exec``.
from app import model_router  # noqa: E402
from app.config import VALID_DOMAINS, settings  # noqa: E402
from app.utils.embedding_cache import truncate_and_normalize  # noqa: E402
from app.utils.milvus_utils import COLLECTION_NAME, get_collection  # noqa: E402

logger = logging.getLogger("scaffold.reindex")


# ---------------------------------------------------------------------------
# The embedding-text builder must match rag_pipeline._build_embedding_text
# byte-for-byte — otherwise re-indexed vectors won't match what
# query-time embed_query produces against ingest-time content. Centralized
# here as the canonical reference; if rag_pipeline ever changes its
# format, this needs to track.
# ---------------------------------------------------------------------------
def _build_embedding_text(entry: dict) -> str:
    parts: list[str] = []
    if entry.get("title"):
        parts.append(entry["title"])
    tags = entry.get("domain_tags") or []
    if tags:
        parts.append(f"Topics: {', '.join(tags)}")
    if entry.get("canonical_text"):
        parts.append(entry["canonical_text"])
    return "\n".join(parts)


# All non-vector fields that toon_v2 carries. Used to round-trip every
# entry through query → upsert without dropping data.
_PRESERVED_FIELDS = (
    "entry_id", "title", "canonical_text", "domain", "domain_tags",
    "confidence_score", "source_type", "source_url", "content_hash",
    "model_id", "version", "supersedes_id", "created_at", "expires_at",
)


# ---------------------------------------------------------------------------
# Core algorithm — tested directly with mocked collection + model_router
# ---------------------------------------------------------------------------
async def reindex_partition(
    collection: Any,
    domain: str,
    *,
    new_embedder: str | None,
    new_provider: str | None,
    batch_size: int,
    dry_run: bool,
    now_ms: int,
    embed_fn=None,
    upsert_fn=None,
) -> dict[str, int]:
    """Re-embed every entry in a single partition.

    ``embed_fn`` / ``upsert_fn`` are injection seams for testing — production
    callers leave them ``None`` and the script wires up ``model_router.embed``
    + ``collection.upsert`` automatically.
    """
    if embed_fn is None:
        embed_fn = _default_embed_fn(new_embedder, new_provider)
    if upsert_fn is None:
        upsert_fn = _default_upsert_fn(collection)

    stats = {"scanned": 0, "reembedded": 0, "skipped_empty": 0, "errors": 0}
    cursor: str | None = None
    expr_base = f'domain == "{domain}"'

    while True:
        expr = expr_base
        if cursor is not None:
            # Milvus-side pagination via the primary key. ``entry_id`` is a
            # VARCHAR so lexicographic ordering is stable enough for a one-
            # shot reindex (we don't care about insertion order).
            expr = f'{expr} and entry_id > "{cursor}"'

        page = collection.query(
            expr=expr,
            output_fields=list(_PRESERVED_FIELDS),
            limit=batch_size,
        )
        if not page:
            break

        # Sort defensively so the cursor advance is monotonic — Milvus's
        # query result order is not guaranteed without an order_by clause.
        page = sorted(page, key=lambda r: r.get("entry_id") or "")
        cursor = page[-1].get("entry_id")
        stats["scanned"] += len(page)

        if dry_run:
            continue

        texts: list[str] = []
        rows_to_embed: list[dict] = []
        for row in page:
            text = _build_embedding_text(row)
            if not text.strip():
                stats["skipped_empty"] += 1
                continue
            texts.append(text)
            rows_to_embed.append(row)

        if not texts:
            continue

        try:
            vectors = await embed_fn(texts)
        except Exception as exc:
            logger.error("embed_call_failed: domain=%s error=%s", domain, exc)
            stats["errors"] += len(texts)
            continue

        if len(vectors) != len(texts):
            logger.error(
                "embed_length_mismatch: domain=%s expected=%d got=%d — skipping batch",
                domain, len(texts), len(vectors),
            )
            stats["errors"] += len(texts)
            continue

        for row, vec in zip(rows_to_embed, vectors):
            if not vec:
                stats["errors"] += 1
                continue
            truncated = truncate_and_normalize(vec)
            payload = _build_upsert_row(row, truncated, new_embedder, now_ms)
            try:
                await upsert_fn(payload)
                stats["reembedded"] += 1
            except Exception as exc:
                logger.warning(
                    "upsert_failed: entry_id=%s error=%s",
                    row.get("entry_id"), exc,
                )
                stats["errors"] += 1

    return stats


def _default_embed_fn(new_embedder: str | None, new_provider: str | None):
    """Wire ``model_router.embed`` with the configured overrides. Built lazily
    so test code can inject its own embed_fn without monkey-patching."""
    overrides: dict[str, str] = {}
    if new_embedder:
        overrides["model_embedder_pipeline"] = new_embedder
    if new_provider:
        overrides["model_embedder_pipeline_provider"] = new_provider

    async def _embed(texts: list[str]) -> list[list[float]]:
        return await model_router.embed(
            texts,
            role="model_embedder_pipeline",
            overrides=overrides or None,
        )
    return _embed


def _default_upsert_fn(collection):
    """Run sync ``collection.upsert`` on a worker thread to keep the event
    loop unblocked (PyMilvus is sync — invariant #4 in the project rules)."""
    loop = asyncio.get_event_loop()

    async def _upsert(row: dict) -> None:
        await loop.run_in_executor(None, lambda: collection.upsert([row]))
    return _upsert


def _build_upsert_row(
    src: dict,
    new_vector: list[float],
    new_embedder: str | None,
    now_ms: int,
) -> dict:
    """Carry every preserved field through; only vector / model_id / updated_at
    change. ``model_id`` records which embedder produced the new vector — set
    to ``new_embedder`` when supplied, else the current settings value."""
    out: dict[str, Any] = {f: src.get(f) for f in _PRESERVED_FIELDS}
    out["dense_vector"] = new_vector
    out["model_id"] = new_embedder or settings.model_embedder_id
    out["updated_at"] = now_ms
    return out


async def reindex_all(
    *,
    new_embedder: str | None,
    new_provider: str | None,
    domain: str | None,
    batch_size: int,
    dry_run: bool,
    collection=None,
) -> dict[str, dict[str, int]]:
    """Run reindex_partition across every selected domain.

    Returns ``{domain: stats}`` for the caller to render. ``collection`` is
    injectable for testing; production passes ``None`` and we resolve via
    ``get_collection``.
    """
    if collection is None:
        collection = get_collection(raise_on_missing=True)

    targets = [domain] if domain else sorted(VALID_DOMAINS)
    now_ms = int(time.time() * 1000)
    out: dict[str, dict[str, int]] = {}
    for d in targets:
        logger.info("reindex_partition_start: domain=%s", d)
        stats = await reindex_partition(
            collection, d,
            new_embedder=new_embedder,
            new_provider=new_provider,
            batch_size=batch_size,
            dry_run=dry_run,
            now_ms=now_ms,
        )
        out[d] = stats
        logger.info("reindex_partition_done: domain=%s stats=%s", d, stats)
    return out


# ---------------------------------------------------------------------------
# cache_metadata.active_embedder_id post-reindex update — §17.155 follow-up #2
# ---------------------------------------------------------------------------
async def _record_active_embedder(new_id: str) -> None:
    """UPSERT the new embedder identity into ``cache_metadata`` so the
    lifespan drift check (``app/utils/embedder_drift.py``) sees
    ``outcome='unchanged'`` on the next boot. Without this, the first boot
    after a reindex fires a spurious ``cache.embedder_drift`` CRITICAL
    alert (stored = old id, configured = new id) even though the corpus
    was just re-embedded to match the new id.

    Mirrors the upsert in ``embedder_drift.check_embedder_drift``."""
    from sqlalchemy import text
    from app.database import async_session
    async with async_session() as db:
        await db.execute(
            text(
                "INSERT INTO cache_metadata (key, value) "
                "VALUES (:k, :v) "
                "ON CONFLICT (key) DO UPDATE "
                "  SET value = EXCLUDED.value, updated_at = NOW()"
            ),
            {"k": "active_embedder_id", "v": new_id},
        )
        await db.commit()


# ---------------------------------------------------------------------------
# CLI wrapper
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reindex",
        description=(
            "Re-embed every entry in toon_v2 with a different embedder. "
            "Run inside the orchestrator container."
        ),
    )
    p.add_argument(
        "--new-embedder", default=None,
        help="Embedder model tag (default: current MODEL_EMBEDDER_PIPELINE).",
    )
    p.add_argument(
        "--new-provider", default=None, choices=["ollama", "openai"],
        help="Provider for the embedder role (default: current setting).",
    )
    p.add_argument(
        "--domain", default=None, choices=sorted(VALID_DOMAINS),
        help="Restrict to one partition. Default: fan out across all 5.",
    )
    p.add_argument(
        "--batch-size", type=int, default=32,
        help="Embeddings per provider call (default 32).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Count entries that would be re-embedded; make no changes.",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Skip the destructive-operation confirmation prompt.",
    )
    return p


def _confirm(msg: str) -> bool:
    print(msg, flush=True)
    try:
        ans = input("Continue? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    print(f"Collection: {COLLECTION_NAME}")
    print(f"  scope:    {'domain=' + args.domain if args.domain else 'all 5 domains'}")
    print(f"  embedder: {args.new_embedder or settings.model_embedder_pipeline}"
          f" ({args.new_provider or 'current provider'})")
    print(f"  batch:    {args.batch_size}")
    print(f"  mode:     {'DRY RUN' if args.dry_run else 'LIVE upsert'}")
    print()

    if not args.dry_run and not args.yes:
        if not _confirm(
            "This rewrites every entry's vector. The cosine geometry of "
            "the entire corpus changes when the embedder changes — old "
            "queries against new vectors won't return what they used to.\n"
            "Run with --dry-run first if you haven't already."
        ):
            print("Aborted.")
            return 1

    stats = asyncio.run(reindex_all(
        new_embedder=args.new_embedder,
        new_provider=args.new_provider,
        domain=args.domain,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    ))

    # §17.155 follow-up #2 — record the new embedder identity in
    # cache_metadata so the next lifespan boot sees outcome='unchanged'
    # instead of firing a spurious cache.embedder_drift CRITICAL alert.
    # Mirrors the upsert pattern in app/utils/embedder_drift.py. Only
    # runs on a live (non-dry-run) reindex with no errors — a partial
    # rewrite must not advance the recorded identity.
    if not args.dry_run and not totals["errors"]:
        new_id = args.new_embedder or settings.model_embedder_id
        try:
            asyncio.run(_record_active_embedder(new_id))
            print(f"\nRecorded active_embedder_id={new_id!r} in cache_metadata "
                  f"(prevents spurious drift alert on next boot).")
        except Exception as exc:
            logger.warning(
                "active_embedder_id_record_failed: model=%s err=%s — "
                "the next lifespan boot may emit a spurious "
                "cache.embedder_drift alert; re-run with --yes after "
                "fixing the DB connectivity, or manually UPSERT into "
                "cache_metadata.",
                new_id, exc,
            )

    print()
    print("Per-partition stats:")
    totals = {"scanned": 0, "reembedded": 0, "skipped_empty": 0, "errors": 0}
    for d, s in stats.items():
        print(f"  {d:<6}  scanned={s['scanned']:>6}  "
              f"reembedded={s['reembedded']:>6}  "
              f"skipped_empty={s['skipped_empty']:>4}  errors={s['errors']:>3}")
        for k in totals:
            totals[k] += s[k]
    print(f"  {'TOTAL':<6}  scanned={totals['scanned']:>6}  "
          f"reembedded={totals['reembedded']:>6}  "
          f"skipped_empty={totals['skipped_empty']:>4}  "
          f"errors={totals['errors']:>3}")

    if args.dry_run:
        print(f"\nDry run complete. Re-run without --dry-run to apply.")
    else:
        print(f"\nReindex complete. Update MODEL_EMBEDDER_PIPELINE in .env "
              f"and `make restart` to switch the live embedder.")

    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
