"""§17.1081 — the engine's optional capabilities, as self-setup walkthroughs.

``GET /setup/recipes`` lists them with their DETECTED status; ``POST
/setup/recipes/{id}/start`` opens the walkthrough as an ordinary job (Phase 1
refine → approve with Assist → guided steps). Mounted in ``app/main.py``;
inherits the global ``Depends(require_api_key)``. Starting a recipe changes
the deployment, so it is admin-only like the models wizard.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import Principal, get_principal, require_admin
from app.database import get_db
from app.modules import engine_setup

router = APIRouter(tags=["Setup"])


@router.get("/setup/recipes")
async def list_setup_recipes(db: AsyncSession = Depends(get_db)) -> dict:
    """Every optional capability with its live status (on / off / blocked /
    in_progress / manual), the plain-words reason it ships off, and the job
    carrying its walkthrough when one is open."""
    return {"recipes": await engine_setup.list_recipes(db)}


@router.post("/setup/recipes/{recipe_id}/start", dependencies=[Depends(require_admin)])
async def start_setup_recipe(
    recipe_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> dict:
    """Open the walkthrough: returns ``{job_id, status: "refining"}`` — the
    same shape as ``POST /ideate/start`` so the console lands on the job hub."""
    if recipe_id not in engine_setup.BY_ID:
        raise HTTPException(status_code=404, detail=f"unknown setup recipe: {recipe_id}")
    try:
        return await engine_setup.start_recipe(db, recipe_id, owner=principal.identity)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# §17.1146 — the target machine fetches the helper FROM the engine (the operator
# has one shell, on the target; scp from the engine host needs a second one).
# Unauthenticated on purpose: it is the Apache-licensed script from the public
# repo, nothing else, and the target has no API key at hand.
@router.get("/setup/runner/local_runner_mcp.py", include_in_schema=False)
async def serve_local_runner_script():
    from pathlib import Path
    from fastapi.responses import FileResponse
    path = Path(__file__).resolve().parents[2] / "scripts" / "local_runner_mcp.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="local_runner_mcp.py is not shipped in this image")
    return FileResponse(str(path), media_type="text/x-python", filename="local_runner_mcp.py")
