"""Artifact read endpoints (§17.565).

GET /jobs/{job_id}/artifacts  — list a job's persisted deliverables
GET /artifacts/{artifact_id}  — fetch one artifact (with content)

Write side lives in app/modules/artifacts.py (called on job finalization).
Auth is inherited from the global require_api_key dependency on the app
(app/main.py) — no per-route dependency needed, same as the other routers.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.database import get_db
from app.schemas import ArtifactListResponse, ArtifactRead

logger = logging.getLogger("scaffold.routers.artifacts")
router = APIRouter()

_ARTIFACT_COLS = (
    "id, job_id, node_id, artifact_type, title, content, file_path, "
    "mime_type, size_bytes, metadata, created_at"
)

# §17.611 (audit #17) — the LIST view omits the (potentially large) content
# column: persist_job_artifacts writes the whole compiled_output as one
# job-level artifact plus one 'code' artifact per CodeGen node, so selecting
# content for every row materialized + serialized every deliverable body when
# the list only needs metadata. `content` is Optional (defaults None); full
# content stays available via GET /artifacts/{artifact_id}. size_bytes is kept
# so callers still see each artifact's size.
_ARTIFACT_LIST_COLS = (
    "id, job_id, node_id, artifact_type, title, file_path, "
    "mime_type, size_bytes, metadata, created_at"
)


def _require_uuid(raw: str, field: str) -> str:
    try:
        return str(UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail=f"{field} must be a UUID")


@router.get("/jobs/{job_id}/artifacts")
async def list_job_artifacts(
    job_id: str, db=Depends(get_db),
) -> ArtifactListResponse:
    """List a job's artifacts (job-level deliverable first, then per-node)."""
    job_id = _require_uuid(job_id, "job_id")
    rows = (await db.execute(
        text(
            f"SELECT {_ARTIFACT_LIST_COLS} FROM artifacts "
            "WHERE job_id = :jid "
            "ORDER BY node_id NULLS FIRST, created_at"
        ),
        {"jid": job_id},
    )).mappings().all()
    artifacts = [ArtifactRead.model_validate(dict(r)) for r in rows]
    logger.info("artifacts_listed job_id=%s count=%d", job_id, len(artifacts))
    return ArtifactListResponse(artifacts=artifacts, total=len(artifacts))


@router.get("/artifacts/{artifact_id}")
async def get_artifact(
    artifact_id: str, db=Depends(get_db),
) -> ArtifactRead:
    """Fetch a single artifact, including its full content."""
    artifact_id = _require_uuid(artifact_id, "artifact_id")
    row = (await db.execute(
        text(f"SELECT {_ARTIFACT_COLS} FROM artifacts WHERE id = :aid"),
        {"aid": artifact_id},
    )).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return ArtifactRead.model_validate(dict(row))
