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
# §17.457 — lifecycle stepper
# ---------------------------------------------------------------------------

# The user-facing pipeline collapses the internal 9-state machine into the six
# phases people actually care about. Each entry maps a phase label to the raw
# job statuses that live under it.
_PIPELINE_STEPS: tuple[tuple[str, frozenset[str]], ...] = (
    ("Refine", frozenset({"pending", "refining"})),
    ("Review", frozenset({"awaiting_confirmation"})),
    ("Research", frozenset({"researching"})),
    ("Plan", frozenset({"planning"})),
    ("Execute", frozenset({"executing", "running"})),
    ("Done", frozenset({"completed"})),
)
_TERMINAL_ERROR_STATUSES = frozenset({"failed", "cancelled", "blocked"})


def _pipeline_steps(status: str, *, has_nodes: bool) -> list[dict]:
    """Build the lifecycle stepper for a job status.

    Returns one dict per phase: ``{"label": str, "state": str}`` where state is
    ``done`` | ``current`` | ``upcoming`` | ``error``. Driven purely by the job
    status (+ whether a DAG exists) so it recomputes correctly on every §17.455
    poll.

    Terminal-error states (failed/cancelled/blocked) don't record which phase
    they died in, so the errored step is a best-effort heuristic: a job that
    reached a DAG (``has_nodes``) failed at Execute; otherwise it failed early,
    marked at Refine. The precise reason is always carried by the §17.450
    error banner regardless of where the marker lands.
    """
    if status == "completed":
        return [{"label": label, "state": "done"} for label, _ in _PIPELINE_STEPS]

    if status in _TERMINAL_ERROR_STATUSES:
        err_idx = 4 if (status == "blocked" or has_nodes) else 0
        out = []
        for i, (label, _) in enumerate(_PIPELINE_STEPS):
            state = "done" if i < err_idx else "error" if i == err_idx else "upcoming"
            out.append({"label": label, "state": state})
        return out

    current = 0
    for i, (_, statuses) in enumerate(_PIPELINE_STEPS):
        if status in statuses:
            current = i
            break
    out = []
    for i, (label, _) in enumerate(_PIPELINE_STEPS):
        state = "done" if i < current else "current" if i == current else "upcoming"
        out.append({"label": label, "state": state})
    return out


def _job_context(payload: dict, job_id: str, *, autorun: bool = False) -> dict:
    """Shared template context for the detail page + poll fragment (so the
    stepper and autorun flag stay in sync across both render paths)."""
    has_nodes = bool(payload.get("nodes")) or bool(payload.get("total_nodes"))
    return {
        "job": payload,
        "job_id": job_id,
        "autorun": autorun,
        "steps": _pipeline_steps(payload.get("job_status", ""), has_nodes=has_nodes),
    }


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
    run: int = 0,
    client=Depends(get_sdk_client),
):
    """Per-job detail: status, nodes, compiled_output, synthesis flags.

    §17.450 — `def` (not `async def`): same single-worker loopback-deadlock fix
    as jobs_list. The body's only I/O is the blocking sync `client.jobs.status`.

    §17.456 — ``?run=1`` (set by the confirm redirect) carries auto-run intent:
    when the job reaches `planning`, the page auto-starts the SSE execution
    stream instead of showing the manual "Run all nodes" button. The flag is
    threaded through §17.455's poll so it survives the `researching` wait.
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
        _job_context(payload, job_id, autorun=bool(run)),
    )


@router.get(
    "/jobs/{job_id}/fragment", response_class=HTMLResponse, dependencies=[],
)
def job_detail_fragment(
    request: Request,
    job_id: str,
    run: int = 0,
    client=Depends(get_sdk_client),
):
    """§17.455 — htmx poll target for the live job-detail page.

    Returns just the job-detail root (``_job_detail_root.html`` — the same wrapper
    + body the full page renders), so a poll can ``hx-swap="outerHTML"`` it in
    place. The root re-emits its own polling trigger only while the job stays in a
    transient state (pending/refining/researching); once it transitions, the
    swapped-in markup omits the trigger and htmx stops polling.

    §17.450 — ``def`` (not ``async def``): blocking sync loopback call, same
    single-worker deadlock fix as ``job_detail``.

    On any fetch error we return a bare, non-polling section so the poll loop
    halts gracefully instead of hammering a broken backend every 3s.
    """
    try:
        payload = client.jobs.status(job_id)
    except Exception as exc:
        logger.exception(
            "web_job_fragment_failed: job=%s error=%s", job_id, exc,
        )
        payload = None

    if not isinstance(payload, dict) or "error" in payload:
        return HTMLResponse(
            '<section class="job-detail" id="job-detail-root">'
            '<p class="job-error-banner">⚠ Lost contact with this job — '
            '<a href="/web/jobs/' + job_id + '">reload</a>.</p></section>'
        )

    return templates.TemplateResponse(
        request, "web/_job_detail_root.html",
        _job_context(payload, job_id, autorun=bool(run)),
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
def post_ideate(
    request: Request,
    idea: str = Form(...),
    domain: str | None = Form(None),
    client=Depends(get_sdk_client),
):
    """Sprint J.2.b / §17.454 — submit Phase 1 and redirect to the LIVE job page.

    Pre-§17.454 this fired ``/ideate`` (synchronous, 100-547s) as a background
    task and redirected to ``/web/jobs?status=refining`` — the user then had to
    hunt for their own just-submitted job in a filtered list because the job_id
    wasn't known at redirect time. Now we call ``/ideate/start``, which creates
    the row and returns its ``job_id`` in milliseconds while running the
    refinement in an orchestrator-side background task. We redirect straight to
    ``/web/jobs/{job_id}`` so the user lands on their own job's detail page and
    watches it progress.

    §17.450 — ``def`` (not ``async def``): the body makes a BLOCKING sync loopback
    call via the SDK Client. On the single-worker event loop an ``async def`` here
    deadlocks (handler blocks the loop waiting on its own loopback). FastAPI runs
    ``def`` routes in a threadpool, so the sync call no longer blocks the loop.
    The call is fast now (INSERT + task spawn), so the short read client suffices.
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

    try:
        result = client.ideate_start(idea=idea_text, domain=domain_clean)
    except Exception as exc:
        logger.exception("web_ideate_start_failed: error=%s", exc)
        return templates.TemplateResponse(
            request, "web/error.html",
            {"error": str(exc), "title": "Could not submit idea"},
            status_code=502,
        )

    job_id = result.get("job_id") if isinstance(result, dict) else None
    if not job_id:
        return templates.TemplateResponse(
            request, "web/error.html",
            {
                "error": "Orchestrator did not return a job id.",
                "title": "Could not submit idea",
            },
            status_code=502,
        )
    # Land the user on their own job's live detail page.
    return RedirectResponse(url=f"/web/jobs/{job_id}", status_code=302)


@router.post("/jobs/{job_id}/confirm", dependencies=[])
async def post_confirm(
    request: Request,
    job_id: str,
    background_tasks: BackgroundTasks,
    feedback: str | None = Form(None),
    long_client=Depends(get_sdk_long_client),
):
    """Sprint J.2.b / §17.456 — kick off Phase 2 (research → ingest → compile)
    as a background task, then auto-chain into execution (parity with the chat
    `/confirm` macro).

    Phase 2 takes 512-1450s, so it stays a background task. We redirect to the
    detail page with ``?run=1``: §17.455's live poll watches the
    `awaiting_confirmation → researching → planning` transition, and the ``run=1``
    flag (carried through the poll) makes the page auto-start the SSE execution
    stream the moment the job reaches `planning` — no manual "Run all nodes"
    click. ``/execute/all`` auto-generates the DAG if missing, so (unlike the chat
    macro) no separate `/dag` step is needed.
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
        url=f"/web/jobs/{job_id}?run=1", status_code=302,
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
