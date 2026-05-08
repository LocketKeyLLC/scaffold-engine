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

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
_long_client_singleton = None


def _build_client(*, timeout: int):
    """Construct a scaffold_client.Client with the given timeout.

    Imports are local to keep the web package importable even if the
    SDK isn't on the path (e.g. some test environments). The base URL
    points at the orchestrator's own port — same process, loopback HTTP.
    """
    from scaffold_client import Client
    return Client(
        base_url=settings.web_loopback_url,
        api_key=settings.scaffold_api_key.get_secret_value(),
        timeout=timeout,
    )


def get_sdk_client():
    """FastAPI dependency: yields a memoized SDK Client used by **read**
    routes (jobs list / detail). Short timeout (default 30s).

    Tests can override via ``app.dependency_overrides[get_sdk_client]``
    to inject a mock without needing the orchestrator running.
    """
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = _build_client(timeout=settings.web_loopback_timeout)
    return _client_singleton


def get_sdk_long_client():
    """FastAPI dependency: yields a memoized SDK Client used by **submit**
    routes (ideate / confirm). Long timeout (default 1800s) covers the
    100-547s Phase 1 + 512-1450s Phase 2 worst cases per the perf table.

    Distinct singleton from ``get_sdk_client`` so a long-running ideate
    can't tie up the read path's connection pool. Tests substitute via
    ``app.dependency_overrides[get_sdk_long_client]``.
    """
    global _long_client_singleton
    if _long_client_singleton is None:
        _long_client_singleton = _build_client(
            timeout=settings.web_loopback_long_timeout,
        )
    return _long_client_singleton


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


# ---------------------------------------------------------------------------
# Submit flow (J.2.b)
# ---------------------------------------------------------------------------

# Allow-list for the domain dropdown — must match ALLOWED_DOMAINS in
# app.modules.idea_refinement. Duplicated here as a literal so the form
# template can render the options without importing the orchestrator
# module (loopback discipline).
_ALLOWED_DOMAINS = ("prompt", "rag", "llm", "spec", "eng")


@router.get("/new", response_class=HTMLResponse, dependencies=[])
async def new_idea_form(request: Request):
    """Sprint J.2.b — render the idea-submission form."""
    return templates.TemplateResponse(
        request, "web/new.html",
        {"allowed_domains": _ALLOWED_DOMAINS},
    )


@router.post("/ideate", dependencies=[])
async def post_ideate(
    request: Request,
    background_tasks: BackgroundTasks,
    idea: str = Form(...),
    domain: str | None = Form(None),
    long_client=Depends(get_sdk_long_client),
):
    """Sprint J.2.b — kick off Phase 1 ideate as a background task.

    Phase 1 takes 100-547s per the perf table; we can't block the
    browser request that long. Background-task pattern: queue the SDK
    call, redirect the browser to ``/web/jobs?status=refining`` so the
    user can watch the new job appear in the list.

    The job_id is *not* known at redirect time — the orchestrator's
    /ideate endpoint creates the row and runs the LLM in one synchronous
    call. The user clicks into the job from the filtered list once it
    appears.
    """
    idea_text = (idea or "").strip()
    if not idea_text:
        return templates.TemplateResponse(
            request, "web/new.html",
            {
                "allowed_domains": _ALLOWED_DOMAINS,
                "error": "Idea is required.",
                "idea_value": idea,
                "domain_value": domain,
            },
            status_code=422,
        )
    domain_clean = (domain or "").strip() or None
    if domain_clean is not None and domain_clean not in _ALLOWED_DOMAINS:
        return templates.TemplateResponse(
            request, "web/new.html",
            {
                "allowed_domains": _ALLOWED_DOMAINS,
                "error": f"Invalid domain: {domain_clean}",
                "idea_value": idea_text,
                "domain_value": domain,
            },
            status_code=422,
        )

    def _kick_off():
        try:
            long_client.ideate(idea=idea_text, domain=domain_clean)
        except Exception as exc:
            logger.exception(
                "web_ideate_background_failed: error=%s", exc,
            )

    background_tasks.add_task(_kick_off)
    # Redirect to the refining filter so the user sees the new job
    # appear once the orchestrator inserts it.
    return RedirectResponse(url="/web/jobs?status=refining", status_code=302)


@router.post("/jobs/{job_id}/confirm", dependencies=[])
async def post_confirm(
    request: Request,
    job_id: str,
    background_tasks: BackgroundTasks,
    feedback: str | None = Form(None),
    long_client=Depends(get_sdk_long_client),
):
    """Sprint J.2.b — kick off Phase 2 (research → ingest → compile) as
    a background task.

    Phase 2 takes 512-1450s. Same background-task pattern as ideate:
    queue the SDK call, redirect to the job-detail page so the user
    can watch the status transition `awaiting_confirmation` →
    `researching` → `planning` via page refresh.
    """
    feedback_clean = (feedback or "").strip() or None

    def _kick_off():
        try:
            long_client.confirm(job_id, feedback=feedback_clean)
        except Exception as exc:
            logger.exception(
                "web_confirm_background_failed: job=%s error=%s",
                job_id, exc,
            )

    background_tasks.add_task(_kick_off)
    return RedirectResponse(
        url=f"/web/jobs/{job_id}", status_code=302,
    )
