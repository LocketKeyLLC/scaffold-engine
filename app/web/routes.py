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

import html as _html_lib

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
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
_async_long_client_singleton = None


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


def _build_async_long_client():
    """Sprint J.2.c — async SDK Client for SSE streaming. The sync Client
    can't async-iterate; ``aiter_execute_all`` lives on AsyncClient."""
    from scaffold_client import AsyncClient
    return AsyncClient(
        base_url=settings.web_loopback_url,
        api_key=settings.scaffold_api_key.get_secret_value(),
        timeout=settings.web_loopback_long_timeout,
    )


def get_sdk_async_long_client():
    """FastAPI dependency: memoized AsyncClient for SSE-streaming routes
    (J.2.c — execute progress). Tests override via
    ``app.dependency_overrides[get_sdk_async_long_client]``."""
    global _async_long_client_singleton
    if _async_long_client_singleton is None:
        _async_long_client_singleton = _build_async_long_client()
    return _async_long_client_singleton


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse, dependencies=[])
def jobs_list(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
    client=Depends(get_sdk_client),
):
    """Paginated jobs list. ``?status=`` and ``?q=`` filter via the SDK.

    §17.450 — `def` (not `async def`): the body does a BLOCKING loopback call
    via the sync SDK Client. As `async def` on the single-worker event loop it
    deadlocked — the handler blocked the loop waiting for its own loopback
    `/jobs`, which the same loop then couldn't serve (30 s → 502). FastAPI runs
    `def` routes in a threadpool, so the sync call no longer blocks the loop.
    """
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
def job_detail(
    request: Request,
    job_id: str,
    client=Depends(get_sdk_client),
):
    """Per-job detail: status, nodes, compiled_output, synthesis flags.

    §17.450 — `def` (not `async def`): same single-worker loopback-deadlock fix
    as jobs_list. The body's only I/O is the blocking sync `client.jobs.status`.
    """
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
_ALLOWED_DOMAINS = ("prompt", "rag", "llm", "spec", "eng", "eng_design")  # §17.329 — eng_design split for circuit/EDA content


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


# ---------------------------------------------------------------------------
# Execute SSE flow (J.2.c)
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/run", response_class=HTMLResponse, dependencies=[])
async def post_run(request: Request, job_id: str):
    """Sprint J.2.c — replace the trigger button with the SSE-listening
    container. The browser-side ``hx-ext="sse" sse-connect`` then opens
    a GET to ``/web/jobs/{id}/run/stream`` which proxies the orchestrator's
    ``/execute/all`` SSE.

    Returns just the run-section fragment so HTMX can ``hx-swap="outerHTML"``
    it onto the existing ``#run-section`` div on the detail page.
    """
    return templates.TemplateResponse(
        request, "web/_run_section_streaming.html",
        {"job_id": job_id},
    )


def _render_event_html(event_name: str, data: dict) -> str:
    """Render one SSE event as a single-line ``<li>`` HTML fragment.

    Single-line is required so the SSE ``data:`` line carries it without
    multi-line continuation. ``html.escape`` defends against operator-
    supplied node titles / errors that might contain ``<`` / ``>``.
    """
    esc = _html_lib.escape

    if event_name == "node_start":
        node_key = esc(str(data.get("node_key", "")))
        title = esc(str(data.get("title", "")))
        return (
            f'<li class="run-event run-event-start">'
            f'<span class="event-icon">▶</span> '
            f'<code>{node_key}</code> {title} '
            f'<span class="event-meta">running…</span></li>'
        )
    if event_name == "node_done":
        node_key = esc(str(data.get("node_key", "")))
        title = esc(str(data.get("title", "")))
        verified = data.get("verified")
        verified_badge = (
            ' <span class="event-meta event-verified">verified</span>'
            if verified else ""
        )
        return (
            f'<li class="run-event run-event-done">'
            f'<span class="event-icon">✓</span> '
            f'<code>{node_key}</code> {title}{verified_badge}</li>'
        )
    if event_name == "node_failed":
        node_key = esc(str(data.get("node_key", "")))
        title = esc(str(data.get("title", "")))
        err = esc(str(data.get("error") or data.get("verification_reason") or ""))
        return (
            f'<li class="run-event run-event-failed">'
            f'<span class="event-icon">✗</span> '
            f'<code>{node_key}</code> {title} '
            f'<span class="event-meta event-error">{err}</span></li>'
        )
    if event_name == "node_retry":
        node_key = esc(str(data.get("node_key", "")))
        budget = esc(str(data.get("budget_remaining", "?")))
        return (
            f'<li class="run-event run-event-retry">'
            f'<span class="event-icon">↻</span> retrying '
            f'<code>{node_key}</code> '
            f'<span class="event-meta">budget remaining: {budget}</span></li>'
        )
    if event_name == "pipeline_complete":
        passed = esc(str(data.get("passed", "?")))
        failed = esc(str(data.get("failed", "?")))
        total = esc(str(data.get("total_nodes", "?")))
        return (
            f'<li class="run-event run-event-complete">'
            f'<span class="event-icon">✦</span> pipeline complete: '
            f'{passed}/{total} passed, {failed} failed.</li>'
        )
    if event_name in ("error", "blocked", "execution_failed"):
        msg = esc(str(data.get("message") or data.get("error") or event_name))
        return (
            f'<li class="run-event run-event-error">'
            f'<span class="event-icon">⚠</span> {esc(event_name)}: {msg}</li>'
        )
    # Unknown event types pass through with minimal formatting so
    # operators can spot SDK additions that haven't been mapped yet.
    detail = esc(str(data)[:200])
    return (
        f'<li class="run-event run-event-other">'
        f'<span class="event-icon">·</span> {esc(event_name)}: {detail}</li>'
    )


_TERMINAL_EVENTS = frozenset({
    "pipeline_complete", "error", "blocked", "execution_failed",
    "execution_cancelled",
})


@router.get("/jobs/{job_id}/run/stream", dependencies=[])
async def run_stream(
    request: Request,
    job_id: str,
    async_long_client=Depends(get_sdk_async_long_client),
):
    """Sprint J.2.c — proxy the orchestrator's /execute/all SSE.

    Each event from ``async_long_client.aiter_execute_all`` is rendered
    as a single-line ``<li>`` HTML fragment and emitted as an SSE
    ``message`` event. HTMX's ``sse-swap="message"`` + ``hx-swap="beforeend"``
    on the listening ``<ul>`` appends each fragment as it arrives.

    Terminal events (pipeline_complete, error, blocked, execution_*)
    cause the generator to break; the EventSource on the browser side
    closes when the response completes.
    """

    async def _gen():
        try:
            async for evt in async_long_client.aiter_execute_all(job_id):
                event_name = evt.get("event") if isinstance(evt, dict) else None
                data = evt.get("data") if isinstance(evt, dict) else {}
                if not isinstance(data, dict):
                    data = {"raw": str(data)}
                line = _render_event_html(event_name or "unknown", data)
                # Single-line data; SSE message format requires `data:`
                # prefix and trailing blank line per event.
                yield f"event: message\ndata: {line}\n\n"
                if event_name in _TERMINAL_EVENTS:
                    return
        except Exception as exc:  # streaming failed mid-flight
            logger.exception(
                "web_run_stream_failed: job=%s error=%s", job_id, exc,
            )
            err_html = _render_event_html("error", {"message": str(exc)})
            yield f"event: message\ndata: {err_html}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
