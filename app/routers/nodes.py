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

from app.authz import Principal, assert_visible, get_principal
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


async def _guard(db: AsyncSession, principal: Principal, job_id: str) -> None:
    """§17.854 (audit A2) — validate + ownership-gate in one step.

    The node CRUD surface is the highest-privilege mutation path in the engine
    (edit prompt templates, delete nodes, reset a completed job back to
    executing) and was the ONE router taking a principal purely for edit
    attribution while doing no visibility check — a non-admin scoped key that
    learned another owner's job UUID could read/edit/delete its nodes. 404 (not
    403) so a non-owner's job is indistinguishable from a missing one, matching
    jobs.py / workflow.py / status.py.
    """
    _valid_uuid(job_id)
    await assert_visible(db, principal, job_id, detail=f"Job {job_id} not found")


def _attributed(principal: Principal, client_value: str | None) -> str:
    """§17.815 (plan 5.3) — server-derived edit attribution.

    A NON-admin scoped key is always attributed by its real identity — the
    client-sent label is ignored, so one user can't stamp edits as another
    (the §17.810 audit-trail requirement). Admin callers (master key,
    single-user installs, trusted loopbacks) keep their client label
    ("web"/"cli"/"operator" — useful surface provenance) with the admin
    identity as the fallback, preserving pre-§17.815 behavior byte-for-byte
    on single-user boxes."""
    if not principal.is_admin:
        return principal.identity
    return (client_value or "").strip() or principal.identity


@router.get("/nodes/{job_id}")
async def node_list(
    job_id: str, db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Full editable node list for the /ui plan editor.

    Returns ``{job_id, job_status, nodes:[{node_key, title, description, status,
    depends_on, execution_order, edit_version, prompt_template, assigned_model,
    tool, is_deliverable, tool_config}]}`` — exactly the columns the PATCH
    surface accepts, plus ``edit_version`` for the optimistic-lock round-trip.
    """
    await _guard(db, principal, job_id)
    return _dispatch(await node_editor.list_nodes(job_id, db))


@router.patch("/nodes/{job_id}/{node_key}")
async def node_edit(
    job_id: str, node_key: str, body: NodeEditInput,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    await _guard(db, principal, job_id)
    data = body.model_dump(exclude_unset=True)
    expected_version = data.pop("expected_version", None)
    edited_by = _attributed(principal, data.pop("edited_by", None))
    return _dispatch(await node_editor.edit_node(
        job_id, node_key, data,
        expected_version=expected_version, edited_by=edited_by, db=db,
    ))


@router.post("/nodes/{job_id}")
async def node_insert(
    job_id: str, body: NodeInsertInput, db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    await _guard(db, principal, job_id)
    spec = body.model_dump(exclude_unset=False)
    edited_by = _attributed(principal, spec.pop("edited_by", None))
    return _dispatch(await node_editor.insert_node(
        job_id, spec, edited_by=edited_by, db=db,
    ))


@router.delete("/nodes/{job_id}/{node_key}")
async def node_delete(
    job_id: str, node_key: str, edited_by: str | None = None,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    await _guard(db, principal, job_id)
    return _dispatch(await node_editor.delete_node(
        job_id, node_key, edited_by=_attributed(principal, edited_by), db=db,
    ))


@router.post("/nodes/{job_id}/reorder")
async def node_reorder(
    job_id: str, body: NodeReorderInput, db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    await _guard(db, principal, job_id)
    return _dispatch(await node_editor.reorder_nodes(
        job_id, body.ordered_keys,
        edited_by=_attributed(principal, body.edited_by), db=db,
    ))


@router.post("/nodes/{job_id}/{node_key}/reset")
async def node_reset(
    job_id: str, node_key: str, body: NodeResetInput | None = None,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    await _guard(db, principal, job_id)
    edited_by = _attributed(principal, body.edited_by if body else None)
    return _dispatch(await node_editor.reset_node(
        job_id, node_key, edited_by=edited_by, db=db,
    ))
