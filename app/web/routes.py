"""Sprint J.2.a — read-only browse routes for the native web UI.

Two routes:
  - ``GET /web/jobs``           — paginated jobs list
  - ``GET /web/jobs/{job_id}``  — per-job detail (status, nodes, output)

Both are auth-bypassed (no ``Depends(require_api_key)``) so a browser
hitting ``localhost:8000/web/jobs`` works without sending headers.
The embedded ``scaffold_client.Client`` carries the API key for the
loopback HTTP call.

The Client is module-level cached to avoid re-instantiating its
``httpx.Client`` per request. Tests substitute it via dependency
injection (``app.dependency_overrides[get_sdk_client] = ...``) so
they don't need a live orchestrator.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings

logger = logging.getLogger("scaffold.web")

# include_in_schema=False on the router excludes every web HTML route
# from OpenAPI — they're browser-facing, not API contract surface.
router = APIRouter(prefix="/web", tags=["web-ui"], include_in_schema=False)

# Note: shared with app/main.py's `templates` instance — both point at
# ``app/templates`` and resolve sub-namespaces via the path string. We
# instantiate our own here so the web routes don't import from main
# (which would make main → web → main a circular dependency).
templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# SDK Client — module-level singleton with DI override hook
# ---------------------------------------------------------------------------

_client_singleton = None


def _build_client():
    """Construct the scaffold_client.Client used by web routes.

    Imports are local to keep the web package importable even if the
    SDK isn't on the path (e.g. some test environments). The base URL
    points at the orchestrator's own port — same process, loopback HTTP.
    """
    from scaffold_client import Client
    return Client(
        base_url=settings.web_loopback_url,
        api_key=settings.scaffold_api_key.get_secret_value(),
        timeout=settings.web_loopback_timeout,
    )


def get_sdk_client():
    """FastAPI dependency: yields a memoized SDK Client.

    Tests can override via ``app.dependency_overrides[get_sdk_client]``
    to inject a mock without needing the orchestrator running.
    """
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = _build_client()
    return _client_singleton


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse, dependencies=[])
async def jobs_list(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
    client=Depends(get_sdk_client),
):
    """Paginated jobs list. ``?status=`` and ``?q=`` filter via the SDK."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1..100")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    try:
        payload = client.jobs.list(
            status=status, q=q, limit=limit, offset=offset,
        )
    except Exception as exc:
        logger.exception("web_jobs_list_failed: %s", exc)
        return templates.TemplateResponse(
            request, "web/error.html",
            {"error": str(exc), "title": "Could not load jobs"},
            status_code=502,
        )

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    total = payload.get("total", 0) if isinstance(payload, dict) else 0
    return templates.TemplateResponse(
        request, "web/jobs_list.html",
        {
            "jobs": jobs, "total": total,
            "limit": limit, "offset": offset,
            "status_filter": status, "q_filter": q or "",
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[])
async def job_detail(
    request: Request,
    job_id: str,
    client=Depends(get_sdk_client),
):
    """Per-job detail: status, nodes, compiled_output, synthesis flags."""
    try:
        payload = client.jobs.status(job_id)
    except Exception as exc:
        logger.exception("web_job_detail_failed: job=%s error=%s", job_id, exc)
        return templates.TemplateResponse(
            request, "web/error.html",
            {"error": str(exc), "title": f"Could not load job {job_id}"},
            status_code=502,
        )

    if not isinstance(payload, dict) or "error" in payload:
        return templates.TemplateResponse(
            request, "web/error.html",
            {
                "error": (payload or {}).get("error", "Job not found"),
                "title": f"Job {job_id}",
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        request, "web/job_detail.html",
        {"job": payload, "job_id": job_id},
    )
