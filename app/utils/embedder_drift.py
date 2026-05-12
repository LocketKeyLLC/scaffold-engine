"""§17.135 — Embedder-identity drift detection.

The 512-dim Milvus collection geometry is locked at schema creation,
but the IDENTITY of the embedder model that produced those vectors is
not. Swapping ``MODEL_EMBEDDER_PIPELINE`` to a same-dim-but-different
model silently breaks retrieval: every historical entry's vector
remains in Milvus, but new query embeddings live in a different vector
space. Cosine similarity goes from "meaningful" to "noise."

This module compares the configured embedder ID against a persisted
``active_embedder_id`` value (stored in ``cache_metadata``). The
helper runs once at lifespan startup, classifies the outcome, and
emits a critical alert when drift is detected. The Milvus collection
itself is NOT auto-reindexed — that's destructive enough to demand an
operator decision. The alert payload points at ``scripts/reindex.py``.

Fail-soft: any DB error skips the check (logs + returns
``outcome="skipped"``). Startup proceeds; the drift just goes
unnoticed until next boot.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger("scaffold.embedder_drift")

_METADATA_KEY = "active_embedder_id"

DriftOutcome = Literal["first_run", "unchanged", "drift", "skipped"]


async def check_embedder_drift(db: AsyncSession) -> dict:
    """Detect embedder-identity drift since the last boot.

    Three normal outcomes + one fail-soft:

    - ``first_run``: ``cache_metadata.active_embedder_id`` was empty.
      Inserts the current id; no alert (this is normal first boot or
      first boot after migration 037).
    - ``unchanged``: stored == current. Touches ``updated_at`` so the
      operator can correlate "last boot that saw embedder X" against
      logs.
    - ``drift``: stored != current. Emits a ``cache.embedder_drift``
      alert (severity=critical, dedup_key tied to the value pair so
      the same drift fires once per cooldown), logs a loud warning,
      and upserts the current id. The Milvus collection is NOT
      touched — operator must run ``scripts/reindex.py``.
    - ``skipped``: DB error during the lookup. The check is best-effort;
      startup continues.
    """
    current = settings.model_embedder_id
    try:
        row = await db.execute(
            text(
                "SELECT value FROM cache_metadata WHERE key = :k"
            ),
            {"k": _METADATA_KEY},
        )
        stored = row.scalar()
    except Exception as exc:
        logger.warning("embedder_drift_check_failed: err=%s", exc)
        return {"outcome": "skipped", "reason": "db_read_failed", "error": str(exc)}

    if stored is None:
        try:
            await db.execute(
                text(
                    "INSERT INTO cache_metadata (key, value) "
                    "VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE "
                    "  SET value = EXCLUDED.value, updated_at = NOW()"
                ),
                {"k": _METADATA_KEY, "v": current},
            )
            await db.commit()
        except Exception as exc:
            logger.warning("embedder_drift_insert_failed: err=%s", exc)
            return {"outcome": "skipped", "reason": "db_write_failed", "error": str(exc)}
        logger.info(
            "embedder_identity_recorded: model=%s (first run / post-migration)",
            current,
        )
        return {"outcome": "first_run", "current": current, "stored": None}

    if stored == current:
        # Touch updated_at so the operator can read "last boot saw this
        # embedder" against historical logs.
        try:
            await db.execute(
                text(
                    "UPDATE cache_metadata SET updated_at = NOW() WHERE key = :k"
                ),
                {"k": _METADATA_KEY},
            )
            await db.commit()
        except Exception as exc:
            logger.debug("embedder_drift_touch_failed: err=%s", exc)
        return {"outcome": "unchanged", "current": current, "stored": stored}

    # ── Drift detected ──────────────────────────────────────────────
    logger.critical(
        "embedder_drift_detected: stored=%s configured=%s — "
        "historical Milvus vectors were embedded by stored model; "
        "new queries will use the configured model. Retrieval quality "
        "will collapse silently until reindex. "
        "Run: docker exec -it scaffold-orchestrator python scripts/reindex.py",
        stored, current,
    )

    # Emit a system_alerts row so the drift surfaces in
    # /observability/alerts and any subscribed sinks. The alert is
    # best-effort — a Redis or DB hiccup must not crash lifespan.
    try:
        from app.observability import alerts as _alerts
        await _alerts.emit(
            kind="cache.embedder_drift",
            severity="critical",
            message=(
                f"Embedder identity changed: stored={stored!r} "
                f"configured={current!r}. Run scripts/reindex.py to "
                f"re-embed the Milvus collection — retrieval quality "
                f"is degraded until then."
            ),
            payload={
                "stored_embedder_id": stored,
                "configured_embedder_id": current,
                "reindex_command": (
                    "docker exec -it scaffold-orchestrator "
                    "python scripts/reindex.py "
                    f"--new-embedder {current}"
                ),
                "embedding_dim": settings.embedding_dim,
            },
            # dedup_key includes the value pair so two different drifts
            # both fire, but the same drift across restarts is rate-
            # limited by alert_cooldown_seconds.
            dedup_key=f"cache.embedder_drift:{stored}->{current}",
            db=db,
        )
    except Exception as exc:
        logger.warning("embedder_drift_alert_failed: err=%s", exc)

    # Upsert the new id so subsequent boots either see "unchanged" (if
    # operator stayed with the new model) or "drift" again (if they
    # toggled back). The upsert MUST succeed even if the alert failed,
    # otherwise every boot would re-fire the same alert.
    try:
        await db.execute(
            text(
                "INSERT INTO cache_metadata (key, value) "
                "VALUES (:k, :v) "
                "ON CONFLICT (key) DO UPDATE "
                "  SET value = EXCLUDED.value, updated_at = NOW()"
            ),
            {"k": _METADATA_KEY, "v": current},
        )
        await db.commit()
    except Exception as exc:
        logger.warning("embedder_drift_upsert_failed: err=%s", exc)
        return {
            "outcome": "drift",
            "current": current,
            "stored": stored,
            "upsert_failed": True,
        }

    return {"outcome": "drift", "current": current, "stored": stored}
