"""Artifact persistence — first-class typed deliverables for a job.

§17.565 — wires up the previously-vestigial ``artifacts`` table. On job
finalization (complete / blocked-partial / autocomplete / assist), the
deliverable is persisted as first-class artifact rows:

  - ONE job-level row from ``jobs.compiled_output`` (artifact_type derived
    from ``deliverable_kind``: ``plan_only`` → ``plan``, else ``report``).
  - ONE per-node ``code`` row for each completed CodeGen node with output,
    and the node's ``dag_nodes.output_artifact_id`` is set to point at it.

This module is the SOLE writer of the ``artifacts`` table and the SOLE
setter of ``dag_nodes.output_artifact_id``. The table has no unique
constraint, so idempotency (safe re-compile / retry) is achieved by an
explicit delete-before-insert. Outputs remain stored inline too
(``dag_nodes.output_text`` + ``jobs.compiled_output``); artifacts are the
typed, fetchable representation layered on top.

Caller commits (mirrors ``provenance.write_provenance``).
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold.artifacts")


def _job_artifact_type(deliverable_kind: str | None) -> str:
    """Map a job's deliverable_kind → the job-level artifact_type.

    ``plan_only`` (unexecuted Shell runbooks) → ``plan``; everything else
    (``executed`` / ``assist_completed`` / None) → ``report``. ``code`` is
    reserved for per-node CodeGen artifacts, the only unambiguous code case.
    """
    return "plan" if deliverable_kind == "plan_only" else "report"


async def persist_job_artifacts(
    job_id: str,
    db: AsyncSession,
    *,
    deliverable_kind: str | None = None,
) -> int:
    """Persist a job's deliverable(s) as artifact rows. Returns the count.

    Idempotent: clears prior artifacts (and the node back-pointers) for the
    job before re-inserting, so a re-compile / retry never accumulates or
    leaves dangling ``output_artifact_id`` references. No-op (returns 0) when
    the job has no ``compiled_output`` yet. Caller commits.
    """
    # 1. Idempotent reset — null the back-pointers first, then drop rows.
    await db.execute(
        text("UPDATE dag_nodes SET output_artifact_id = NULL WHERE job_id = :jid"),
        {"jid": job_id},
    )
    await db.execute(
        text("DELETE FROM artifacts WHERE job_id = :jid"),
        {"jid": job_id},
    )

    # 2. Read the finalized deliverable.
    row = (await db.execute(
        text("SELECT title, compiled_output FROM jobs WHERE id = :jid"),
        {"jid": job_id},
    )).mappings().first()
    if not row or not (row["compiled_output"] or "").strip():
        return 0

    count = 0
    title = row["title"] or "(untitled)"
    compiled = row["compiled_output"]

    # 3. Job-level deliverable row (node_id NULL).
    await db.execute(
        text(
            "INSERT INTO artifacts "
            "(job_id, node_id, artifact_type, title, content, mime_type, "
            " size_bytes, metadata) "
            "VALUES (:jid, NULL, :atype, :title, :content, 'text/markdown', "
            " :size, CAST(:meta AS jsonb))"
        ),
        {
            "jid": job_id,
            "atype": _job_artifact_type(deliverable_kind),
            "title": title,
            "content": compiled,
            "size": len(compiled.encode("utf-8")),
            "meta": json.dumps({"deliverable_kind": deliverable_kind}),
        },
    )
    count += 1

    # 4. Per-node CodeGen rows + output_artifact_id back-pointers.
    #    `tool` is stored exact-case 'CodeGen' (see execution_compile.py).
    node_rows = (await db.execute(
        text(
            "SELECT id, title, output_text FROM dag_nodes "
            "WHERE job_id = :jid AND tool = 'CodeGen' AND status = 'done' "
            "AND output_text IS NOT NULL AND output_text <> ''"
        ),
        {"jid": job_id},
    )).mappings().all()
    for n in node_rows:
        code = n["output_text"]
        aid = (await db.execute(
            text(
                "INSERT INTO artifacts "
                "(job_id, node_id, artifact_type, title, content, mime_type, "
                " size_bytes, metadata) "
                "VALUES (:jid, :nid, 'code', :title, :content, 'text/plain', "
                " :size, CAST(:meta AS jsonb)) "
                "RETURNING id"
            ),
            {
                "jid": job_id,
                "nid": str(n["id"]),
                "title": n["title"] or "code",
                "content": code,
                "size": len(code.encode("utf-8")),
                "meta": json.dumps({"node_id": str(n["id"])}),
            },
        )).scalar()
        await db.execute(
            text("UPDATE dag_nodes SET output_artifact_id = :aid WHERE id = :nid"),
            {"aid": str(aid), "nid": str(n["id"])},
        )
        count += 1

    logger.info(
        "artifacts_persisted job_id=%s count=%d code_nodes=%d kind=%s",
        job_id, count, len(node_rows), deliverable_kind,
    )
    return count
