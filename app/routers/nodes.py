"""§17.478 (Phase 4) — interactive node-control (CRUD) endpoints.

Routes:
  PATCH  /nodes/{job_id}/{node_key}        — node_edit
  POST   /nodes/{job_id}                   — node_insert
  DELETE /nodes/{job_id}/{node_key}        — node_delete
  POST   /nodes/{job_id}/reorder           — node_reorder
  POST   /nodes/{job_id}/{node_key}/reset  — node_reset

Each validates the job_id (400 on malformed) and maps a module-layer
``{"error", "http_status"}`` result to the corresponding HTTPException.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.modules import node_editor
from app.schemas import (
    NodeEditInput,
    NodeInsertInput,
    NodeReorderInput,
    NodeResetInput,
)

router = APIRouter()


def _dispatch(result: dict) -> dict:
    """Raise an HTTPException for a module error result, else return it."""
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 400), detail=result["error"],
        )
    return result


def _valid_uuid(job_id: str) -> None:
    try:
        UUID(job_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id format")


@router.patch("/nodes/{job_id}/{node_key}")
async def node_edit(
    job_id: str, node_key: str, body: NodeEditInput,
    db: AsyncSession = Depends(get_db),
):
    _valid_uuid(job_id)
    data = body.model_dump(exclude_unset=True)
    expected_version = data.pop("expected_version", None)
    edited_by = data.pop("edited_by", None)
    return _dispatch(await node_editor.edit_node(
        job_id, node_key, data,
        expected_version=expected_version, edited_by=edited_by, db=db,
    ))


@router.post("/nodes/{job_id}")
async def node_insert(
    job_id: str, body: NodeInsertInput, db: AsyncSession = Depends(get_db),
):
    _valid_uuid(job_id)
    spec = body.model_dump(exclude_unset=False)
    edited_by = spec.pop("edited_by", None)
    return _dispatch(await node_editor.insert_node(
        job_id, spec, edited_by=edited_by, db=db,
    ))


@router.delete("/nodes/{job_id}/{node_key}")
async def node_delete(
    job_id: str, node_key: str, edited_by: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    _valid_uuid(job_id)
    return _dispatch(await node_editor.delete_node(
        job_id, node_key, edited_by=edited_by, db=db,
    ))


@router.post("/nodes/{job_id}/reorder")
async def node_reorder(
    job_id: str, body: NodeReorderInput, db: AsyncSession = Depends(get_db),
):
    _valid_uuid(job_id)
    return _dispatch(await node_editor.reorder_nodes(
        job_id, body.ordered_keys, edited_by=body.edited_by, db=db,
    ))


@router.post("/nodes/{job_id}/{node_key}/reset")
async def node_reset(
    job_id: str, node_key: str, body: NodeResetInput | None = None,
    db: AsyncSession = Depends(get_db),
):
    _valid_uuid(job_id)
    edited_by = body.edited_by if body else None
    return _dispatch(await node_editor.reset_node(
        job_id, node_key, edited_by=edited_by, db=db,
    ))
