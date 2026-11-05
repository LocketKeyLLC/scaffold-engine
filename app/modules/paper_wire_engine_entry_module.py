# app/modules/paper_wire_engine_entry_module.py
"""Wire the paper-trading lab modules into the scaffold engine.

Exposes ``install(app)`` which registers recurring jobs, mounts an
APIRouter with on-demand POST routes and view GET routes, and starts the
fill-tracker stream consumer as a background task.  No work happens at
import time; no secrets are read inside ``install`` itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI

from app.authz import get_principal
from app.modules.paper_daily_signal_proposal_batch import (
    run_daily_batch,
    run_on_demand_batch,
)
from app.modules.paper_entry_approval import run_approval_entry_point
from app.modules.paper_entry_backtesting import run_backtesting_entry_point
from app.modules.paper_execution_entry_point import run_execution_entry_point
from app.modules.paper_fill_tracker import track_fills
from app.modules.paper_operator_view import get_operator_view
from app.scheduler import get_scheduler

logger = logging.getLogger("scaffold")

LAB_SLUG = "paper"
ROUTER_PREFIX = f"/labs/{LAB_SLUG}"

# Recurring job schedule: daily batch runs once a day before US market open.
DAILY_BATCH_JOB_ID = "paper_daily_batch"
DAILY_BATCH_HOUR = 13  # 13:00 UTC == 9:00 ET (pre-market)
DAILY_BATCH_MINUTE = 0


def _make_router() -> APIRouter:
    """Build the APIRouter for the paper lab's on-demand commands and views."""
    router = APIRouter(prefix=ROUTER_PREFIX, tags=["paper"])

    # On-demand entry points (POST routes).
    @router.post("/daily-batch")
    async def daily_batch(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        result = await run_daily_batch()
        return result

    @router.post("/on-demand-batch")
    async def on_demand_batch(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        result = await run_on_demand_batch()
        return result

    @router.post("/approval")
    async def approval(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        result = await run_approval_entry_point()
        return result

    @router.post("/backtesting")
    async def backtesting(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        result = await run_backtesting_entry_point()
        return result

    @router.post("/execution")
    async def execution(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        result = await run_execution_entry_point()
        return result

    # Views (GET routes).
    @router.get("/view")
    async def view(_principal: Any = Depends(get_principal)) -> dict[str, Any]:
        result = await get_operator_view()
        return result

    return router


def _start_stream_consumer() -> Optional[asyncio.Task]:
    """Start the fill-tracker stream consumer as an asyncio task.

    Returns the created task, or None if no running event loop is
    available (e.g. during synchronous tests).  The task itself reads
    operator secrets and opens a network connection; neither happens
    here, so calling this function never touches secrets or the network.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(track_fills())


def install(app: FastAPI) -> None:
    """Register the paper lab with the engine.

    - Schedules the daily batch with the engine's scheduler (if any).
    - Mounts the APIRouter with on-demand commands and views.
    - Starts the fill-tracker stream consumer as a background task.

    No work happens at import time; no secrets are read here.
    """
    # Recurring job: daily batch before US market open.
    scheduler = get_scheduler()
    if scheduler is not None:
        scheduler.add_job(
            run_daily_batch,
            trigger="cron",
            hour=DAILY_BATCH_HOUR,
            minute=DAILY_BATCH_MINUTE,
            id=DAILY_BATCH_JOB_ID,
            replace_existing=True,
        )

    # On-demand commands and views.
    router = _make_router()
    app.include_router(router)

    # Stream consumer (reads secrets/network only inside the task).
    _start_stream_consumer()
