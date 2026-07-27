"""Top-level natural-language command router (§17.628).

Mounted in ``app/main.py`` via ``app.include_router(route_router)``. Inherits
the global ``Depends(require_api_key)`` from the FastAPI app dependencies — no
per-route auth needed.

Sibling to ``POST /assist/{sid}/interpret`` (§17.626): that endpoint classifies
a turn *inside* an active assist session; this one classifies a top-level plain
message into a read-only engine action so the pipeline can route it without a
slash command. Both are thin wrappers over a fail-soft ``model_router.tool_call``
classifier — see ``app/modules/command_guide.py``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.modules import command_guide

logger = logging.getLogger("scaffold")

router = APIRouter(tags=["Route"])


class RouteInput(BaseModel):
    message: str


@router.post("/route")
async def route_command(body: RouteInput):
    """Classify a plain-language top-level message into an engine intent
    (reads: status / results / rag_query / jobs_* / model_* / schedule_list /
    research_list / research_find / logs / cost / health / config / work_* /
    help; plus the §17.629/§17.630 write & delete verbs), so the OWUI pipeline
    can drive the right component by talking.

    Fail-soft: a classifier hiccup or a non-command message returns
    ``intent='none'`` (confidence ``low``); the pipeline then falls through to
    triage untouched."""
    return await command_guide.classify_command(message=body.message)
