"""Sprint J.2.a — read-only browse routes for the native web UI.

Two routes:
  - ``GET /web/jobs``           — paginated jobs list
  - ``GET /web/jobs/{job_id}``  — per-job detail (status, nodes, output)

Both are auth-bypassed (no ``Depends(require_api_key)``) so a browser
hitting ``localhost:8000/web/jobs`` works without sending headers.
The embedded ``scaffold_client.Client`` carries the API key for the
loopback HTTP call.

§17.810 — MULTI-USER LIMITATION. This console has NO per-browser identity
(no cookie / session / login) and the loopback carries the **master** key, so
every ``/web`` request resolves to the admin principal and sees ALL users'
jobs. It is therefore an **operator/admin console**, not a per-user surface.
In a multi-user deployment (``MULTI_USER_ENABLED=true``) it MUST be
network-restricted (bind to localhost / put it behind an authenticating
reverse proxy) — per-user access is via the direct JSON API and the ``/ui``
SPA, both of which send ``X-API-Key`` and resolve+enforce a Principal
(``app/authz.py``). Building a browser login here is future work.

The Client is module-level cached to avoid re-instantiating its
``httpx.Client`` per request. Tests substitute it via dependency
injection (``app.dependency_overrides[get_sdk_client] = ...``) so
they don't need a live orchestrator.
"""
from __future__ import annotations

import logging

import html as _html_lib

from markdown_it import MarkdownIt

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import get_db  # §17.479 — node-action routes call node_editor in-process

logger = logging.getLogger("scaffold.web")

# include_in_schema=False on the router excludes every web HTML route
# from OpenAPI — they're browser-facing, not API contract surface.
router = APIRouter(prefix="/web", tags=["web-ui"], include_in_schema=False)

# Note: shared with app/main.py's `templates` instance — both point at
# ``app/templates`` and resolve sub-namespaces via the path string. We
# instantiate our own here so the web routes don't import from main
# (which would make main → web → main a circular dependency).
templates = Jinja2Templates(directory="app/templates")
# §17.460 — expose the per-request CSP nonce to web templates (parity with the
# research router) so any future inline <script>/<style> can be nonce'd.
from app.middleware.security_headers import current_csp_nonce as _current_csp_nonce
templates.env.globals["csp_nonce"] = _current_csp_nonce


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


# Single user-facing phase label for ANY job status — backs the /work and
# /here "you-are-here" surfaces (§17.561). Deliberately NOT folded into
# _PIPELINE_STEPS: that tuple's index positions drive the web stepper's
# err_idx=4 heuristic above, so inserting "Assemble" would shift Execute.
# This map covers the statuses the 6-phase stepper doesn't (umbrella
# aggregating, assist*, terminal-error) without disturbing it.
_PHASE_LABEL_EXTRA: dict[str, str] = {
    "aggregating": "Assemble",
    "assisted_executing": "Execute",
    "assisted_running": "Execute",
    "assisted_paused": "Paused",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "blocked": "Blocked",
}


def phase_label_for(status: str) -> str:
    """Return the single user-facing phase label for a job status.

    Reuses the _PIPELINE_STEPS groupings (Refine/Review/Research/Plan/
    Execute/Done) and falls back to _PHASE_LABEL_EXTRA for statuses outside
    the linear stepper. Unknown statuses degrade to a title-cased form.
    """
    for label, statuses in _PIPELINE_STEPS:
        if status in statuses:
            return label
    return _PHASE_LABEL_EXTRA.get(status, status.replace("_", " ").title())


# §17.458 — server-side markdown renderer for the web UI's compiled_output.
# "commonmark" preset for spec-faithful parsing, but html=False so any raw HTML
# in the (LLM/pipeline-generated) output is ESCAPED rather than passed through —
# this is the XSS guard. linkify=False keeps bare URLs as plain text. The
# built-in validateLink also drops javascript:/vbscript:/file:/data: hrefs, so a
# `[x](javascript:...)` never becomes a live link. Output is marked |safe in the
# template only because of these settings — do NOT flip html to True.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False})


def _render_markdown(text: str | None) -> str:
    """Render compiled_output markdown → safe HTML (empty string for falsy input)."""
    if not text:
        return ""
    return _MD.render(text)


def _is_not_found(exc: BaseException) -> bool:
    """True if ``exc`` is the SDK's 404 ``NotFoundError``.

    §17.470 — lets a read route map a genuinely-missing job to HTTP 404 instead
    of the generic 502 "could not load" (the SDK *raises* on 404, so a bare
    ``except Exception`` would otherwise swallow not-found into a gateway error).

    Lazy import mirrors the local-import discipline in ``_build_client``: the SDK
    may be absent in some test environments, so a missing SDK degrades to
    'not a 404' rather than breaking this module's import.
    """
    try:
        from scaffold_client import NotFoundError
    except Exception:
        return False
    return isinstance(exc, NotFoundError)


def _job_context(payload: dict, job_id: str, *, autorun: bool = False) -> dict:
    """Shared template context for the detail page + poll fragment (so the
    stepper, autorun flag, and rendered output stay in sync across both paths)."""
    has_nodes = bool(payload.get("nodes")) or bool(payload.get("total_nodes"))
    return {
        "job": payload,
        "job_id": job_id,
        "autorun": autorun,
        "steps": _pipeline_steps(payload.get("job_status", ""), has_nodes=has_nodes),
        "compiled_output_html": _render_markdown(payload.get("compiled_output")),
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
        if _is_not_found(exc):  # §17.470 — missing job is 404, not a 502 gateway error
            return templates.TemplateResponse(
                request, "web/error.html",
                {"error": "Job not found", "title": f"Job {job_id}"},
                status_code=404,
            )
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
        # §17.470 — escape job_id: this is the one error path that builds raw HTML
        # by string concat instead of a Jinja-autoescaped template, so an
        # attacker-supplied path segment (e.g. ``"><img onerror=...>``) would
        # otherwise be reflected unescaped. quote=True also escapes ``"`` so the
        # value stays inside the href attribute. (CSP blocks inline script, but
        # this restores the escaping discipline documented at _MD above.)
        safe_id = _html_lib.escape(job_id, quote=True)
        return HTMLResponse(
            '<section class="job-detail" id="job-detail-root">'
            '<p class="job-error-banner">⚠ Lost contact with this job — '
            '<a href="/web/jobs/' + safe_id + '">reload</a>.</p></section>'
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


# §17.480 (Slice 3) — new browser surfaces: RAG search, model config, research.
@router.get("/rag", response_class=HTMLResponse, dependencies=[])
async def web_rag(request: Request, q: str | None = None):
    """KB / RAG search. async def + in-process query_rag (no loopback)."""
    results, error = [], None
    query = (q or "").strip()
    if query:
        try:
            from app.modules.rag_pipeline import query_rag
            resp = await query_rag(query, top_k=8)
            if resp.get("status") == "ok":
                results = resp.get("results") or []
            else:
                error = resp.get("error") or "search failed"
        except Exception as exc:  # fail-soft — render the page with an error
            logger.exception("web_rag_failed: q=%s error=%s", query, exc)
            error = str(exc)
    return templates.TemplateResponse(
        request, "web/rag.html",
        {"query": query, "results": results, "error": error},
    )


# §17.483/§17.484 — (field, label, locked) for every role, ordered for display.
# The two locked roles (embedder/reranker) are config-only singletons; the other
# seven are runtime-switchable via POST /web/model and PERSISTED to the
# model_overrides table (§17.484), reloaded onto settings at startup.
_MODEL_ROLE_ROWS = [
    ("model_general", "general", False),
    ("model_router", "router", False),
    ("model_coder", "coder", False),
    ("model_verifier", "verifier", False),
    ("model_fallback", "fallback", False),
    ("model_cloud_heavy", "cloud_heavy", False),
    ("model_cloud_alt", "cloud_alt", False),
    ("model_embedder_pipeline", "embedder (pipeline)", True),
    ("model_reranker", "reranker", True),
]


@router.get("/model", response_class=HTMLResponse, dependencies=[])
def web_model(request: Request, set: str = "", reset: str = "", error: str = ""):
    """View + set the model per role (§17.483/§17.484). Sync — reads the live
    settings singleton and compares each switchable role to its env/config
    default (`config.env_default_model`) to flag/active overrides without a DB
    read. `set`/`reset`/`error` are PRG flash params from the POST routes."""
    from app.config import env_default_model
    roles = []
    for f, label, locked in _MODEL_ROLE_ROWS:
        current = getattr(settings, f)
        env_def = current if locked else env_default_model(f)
        roles.append({
            "field": f, "label": label, "model": current, "locked": locked,
            "env_default": env_def, "overridden": (not locked and current != env_def),
        })
    return templates.TemplateResponse(
        request, "web/model.html",
        {"roles": roles, "flash_set": set, "flash_reset": reset, "flash_error": error},
    )


async def _ollama_tag_exists(model: str) -> bool | None:
    """True/False if `model` is/ isn't a pulled Ollama tag; None if the tag
    list is unreachable (so the caller can fail-soft and allow the set)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            r.raise_for_status()
            names = {m.get("name", "") for m in (r.json().get("models") or [])}
        return model in names
    except Exception as exc:  # connection / timeout / parse — don't block the set
        logger.warning("web_model ollama tag-check unreachable: %s", exc)
        return None


@router.post("/model", dependencies=[])
async def web_model_set(role: str = Form(...), model: str = Form(...), db=Depends(get_db)):
    """§17.484 — re-point a switchable role at `model`, applied to the live
    settings singleton AND persisted to model_overrides (survives restart).
    Validates the role (set_override → set_runtime_model) and that the tag is
    pulled on Ollama (fail-soft if Ollama is unreachable). PRG flash redirect."""
    from urllib.parse import quote
    from app.modules.model_overrides import set_override

    model_clean = (model or "").strip()
    exists = await _ollama_tag_exists(model_clean) if model_clean else False
    if exists is False:
        return RedirectResponse(
            f"/web/model?error={quote(f'model {model_clean!r} is not a pulled Ollama tag')}",
            status_code=302,
        )
    # exists is True (validated) or None (Ollama unreachable → allow).
    try:
        await set_override(role, model_clean, db)
    except ValueError as exc:
        return RedirectResponse(
            f"/web/model?error={quote(str(exc))}", status_code=302,
        )
    return RedirectResponse(f"/web/model?set={quote(role)}", status_code=302)


@router.post("/model/reset", dependencies=[])
async def web_model_reset(role: str = Form(...), db=Depends(get_db)):
    """§17.484 — clear a role's persisted override and revert it to the
    env/config default (deletes the model_overrides row + restores settings)."""
    from urllib.parse import quote
    from app.modules.model_overrides import clear_override
    try:
        await clear_override(role, db)
    except ValueError as exc:
        return RedirectResponse(
            f"/web/model?error={quote(str(exc))}", status_code=302,
        )
    return RedirectResponse(f"/web/model?reset={quote(role)}", status_code=302)


@router.get("/research", response_class=HTMLResponse, dependencies=[])
async def web_research(request: Request, db=Depends(get_db)):
    """Browse recent research sessions + launch form (§17.481)."""
    from sqlalchemy import text as _sql
    rows = (await db.execute(_sql(
        "SELECT id, topic, status, created_at, updated_at "
        "FROM research_sessions ORDER BY created_at DESC LIMIT 25"
    ))).mappings().all()
    return templates.TemplateResponse(
        request, "web/research.html",
        {"sessions": [dict(r) for r in rows], "depths": ["shallow", "medium", "deep"]},
    )


@router.post("/research", dependencies=[])
async def web_research_launch(
    request: Request, topic: str = Form(...), depth: str = Form("medium"),
):
    """§17.481 — fire-and-forget autonomous research, then redirect to the list
    where the new running session appears. Research (20-60 min) runs server-side
    regardless of the browser (spawn_research_background, the §17.454 pattern)."""
    topic_clean = (topic or "").strip()
    if not topic_clean:
        return RedirectResponse("/web/research", status_code=302)
    depth_clean = depth if depth in ("shallow", "medium", "deep") else "medium"
    from app.modules.research_agent import spawn_research_background
    spawn_research_background(topic_clean, depth=depth_clean)
    logger.info("web_research_launched topic=%s depth=%s", topic_clean[:80], depth_clean)
    return RedirectResponse("/web/research", status_code=302)


def _research_session_context(payload: dict | None, session_id: str) -> dict:
    return {
        "s": payload, "session_id": session_id,
        "summary_html": _render_markdown((payload or {}).get("summary")),
    }


async def _research_detail(request: Request, session_id: str, db, *, root_only: bool):
    from uuid import UUID
    from sqlalchemy import text as _sql
    try:
        UUID(session_id)
    except (ValueError, TypeError):
        return templates.TemplateResponse(
            request, "web/error.html",
            {"error": "Invalid session id", "title": "Research"}, status_code=400,
        )
    row = (await db.execute(_sql(
        "SELECT id, topic, depth, domain, status, summary, error_message, "
        "       iterations_completed, total_entries_extracted, total_entries_ingested, "
        "       total_entries_rejected, total_urls_searched, total_queries, "
        "       coverage_pct, duration_ms, created_at, completed_at "
        "FROM research_sessions WHERE id = :id"
    ), {"id": session_id})).mappings().first()
    if not row:
        return templates.TemplateResponse(
            request, "web/error.html",
            {"error": "Research session not found", "title": "Research"},
            status_code=404,
        )
    tmpl = "web/_research_detail_root.html" if root_only else "web/research_detail.html"
    return templates.TemplateResponse(
        request, tmpl, _research_session_context(dict(row), session_id),
    )


@router.get("/research/{session_id}", response_class=HTMLResponse, dependencies=[])
async def web_research_detail(request: Request, session_id: str, db=Depends(get_db)):
    """§17.481 — research session detail (stats + summary + live poll)."""
    return await _research_detail(request, session_id, db, root_only=False)


@router.get(
    "/research/{session_id}/fragment", response_class=HTMLResponse, dependencies=[],
)
async def web_research_detail_fragment(
    request: Request, session_id: str, db=Depends(get_db),
):
    """§17.481 — htmx poll target; re-emits its own poll only while running."""
    return await _research_detail(request, session_id, db, root_only=True)


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
    async_long_client=Depends(get_sdk_async_long_client),
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

    # §17.615 (audit #35) — the confirm runs Phase 2 for 512-1450s. The old form
    # was a SYNC background task (long_client.confirm), which Starlette runs via
    # run_in_threadpool on AnyIO's default 40-token limiter — the SAME pool that
    # serves every sync `def` web route (jobs_list, job_detail, the §17.455 poll),
    # pinning a slot for the whole Phase-2 duration with nothing bounding
    # concurrent confirms. Making _kick_off ASYNC (async client) moves it onto the
    # event loop instead: Starlette awaits async background tasks directly, so no
    # threadpool token is held while the loopback awaits.
    async def _kick_off():
        try:
            await async_long_client.confirm(job_id, feedback=feedback_clean)
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


# §17.479 (Phase 5) — interactive node actions from the job-detail page.
# Unlike the read routes (sync ``def`` + blocking SDK loopback per §17.450),
# these are ``async def`` and call node_editor + execution_status DIRECTLY
# in-process (async DB session) — no loopback, so no single-worker deadlock —
# then re-render the job-detail root for an htmx ``outerHTML`` swap.
async def _node_action_response(request: Request, job_id: str, db):
    from uuid import UUID
    from app.modules.execution_handler import execution_status
    try:
        payload = await execution_status(UUID(job_id), db)
    except (ValueError, TypeError):
        payload = {"error": "Invalid job_id"}
    if not isinstance(payload, dict) or "error" in payload:
        safe_id = _html_lib.escape(job_id, quote=True)
        return HTMLResponse(
            '<section class="job-detail" id="job-detail-root">'
            '<p class="job-error-banner">⚠ Lost contact with this job — '
            '<a href="/web/jobs/' + safe_id + '">reload</a>.</p></section>'
        )
    return templates.TemplateResponse(
        request, "web/_job_detail_root.html", _job_context(payload, job_id),
    )


@router.post(
    "/jobs/{job_id}/nodes/{node_key}/reset",
    response_class=HTMLResponse, dependencies=[],
)
async def web_node_reset(
    request: Request, job_id: str, node_key: str, db=Depends(get_db),
):
    """Re-run a node + its downstream from the web UI."""
    from app.modules import node_editor
    result = await node_editor.reset_node(job_id, node_key, edited_by="web", db=db)
    if isinstance(result, dict) and "error" in result:
        logger.info("web_node_reset_noop job=%s node=%s err=%s",
                    job_id, node_key, result.get("error"))
    return await _node_action_response(request, job_id, db)


@router.post(
    "/jobs/{job_id}/nodes/{node_key}/delete",
    response_class=HTMLResponse, dependencies=[],
)
async def web_node_delete(
    request: Request, job_id: str, node_key: str, db=Depends(get_db),
):
    """Delete a node (dependents rewired) from the web UI."""
    from app.modules import node_editor
    result = await node_editor.delete_node(job_id, node_key, edited_by="web", db=db)
    if isinstance(result, dict) and "error" in result:
        logger.info("web_node_delete_noop job=%s node=%s err=%s",
                    job_id, node_key, result.get("error"))
    return await _node_action_response(request, job_id, db)


@router.get(
    "/jobs/{job_id}/nodes/{node_key}/output",
    response_class=HTMLResponse, dependencies=[],
)
async def web_node_output(
    request: Request, job_id: str, node_key: str, db=Depends(get_db),
):
    """§17.480 — lazy-loaded per-node output (htmx hx-get target). Direct
    in-process read so a large output_text isn't carried in every poll."""
    from uuid import UUID
    from sqlalchemy import text as _sql
    try:
        UUID(job_id)
    except (ValueError, TypeError):
        return HTMLResponse('<div class="node-output empty">(invalid job)</div>')
    row = (await db.execute(
        _sql("SELECT output_text FROM dag_nodes "
             "WHERE job_id = :j AND node_key = :k"),
        {"j": job_id, "k": node_key},
    )).first()
    out = (row[0] if row else None) or ""
    if not out:
        return HTMLResponse('<div class="node-output empty">(no output yet)</div>')
    return HTMLResponse(
        '<div class="node-output markdown-body">' + _render_markdown(out) + "</div>"
    )


# §17.480 (Slice 2) — web node editing parity (edit / insert / reorder).
@router.get(
    "/jobs/{job_id}/nodes/{node_key}/edit",
    response_class=HTMLResponse, dependencies=[],
)
async def web_node_edit_form(
    request: Request, job_id: str, node_key: str, db=Depends(get_db),
):
    """Return the inline edit form for a node (htmx loads it into a slot)."""
    from uuid import UUID
    from sqlalchemy import text as _sql
    try:
        UUID(job_id)
    except (ValueError, TypeError):
        return HTMLResponse('<div class="node-output empty">(invalid job)</div>')
    row = (await db.execute(
        # §17.614 (audit #11) — prefill with prompt_template (the editable field)
        # so the operator edits what execution actually consumes, falling back to
        # optimized_prompt only when the template is empty.
        _sql("SELECT title, tool, COALESCE(is_deliverable, FALSE) AS is_deliverable, "
             "depends_on, COALESCE(prompt_template, optimized_prompt, '') AS prompt, "
             "edit_version FROM dag_nodes WHERE job_id = :j AND node_key = :k"),
        {"j": job_id, "k": node_key},
    )).mappings().first()
    if not row:
        return HTMLResponse('<div class="node-output empty">(node not found)</div>')
    return templates.TemplateResponse(
        request, "web/_node_edit_form.html",
        {"job_id": job_id, "node_key": node_key, "node": dict(row),
         "depends_on_csv": ",".join(row["depends_on"] or [])},
    )


@router.post(
    "/jobs/{job_id}/nodes/{node_key}/edit",
    response_class=HTMLResponse, dependencies=[],
)
async def web_node_edit(
    request: Request, job_id: str, node_key: str,
    title: str = Form(""), tool: str = Form(""),
    depends_on: str = Form(""), prompt_template: str = Form(""),
    is_deliverable: str = Form(""), expected_version: int = Form(None),
    db=Depends(get_db),
):
    from app.modules import node_editor
    # §17.614 (audit #11) — edit prompt_template (the field execution consumes),
    # not optimized_prompt (which the executor regenerates every run).
    fields = {
        "title": title.strip(),
        "tool": tool.strip(),
        "prompt_template": prompt_template,
        "depends_on": [d.strip() for d in depends_on.split(",") if d.strip()],
        "is_deliverable": is_deliverable == "on",
    }
    result = await node_editor.edit_node(
        job_id, node_key, fields,
        expected_version=expected_version, edited_by="web", db=db,
    )
    if isinstance(result, dict) and "error" in result:
        logger.info("web_node_edit_noop job=%s node=%s err=%s",
                    job_id, node_key, result.get("error"))
    return await _node_action_response(request, job_id, db)


@router.get(
    "/jobs/{job_id}/nodes/{node_key}/edit/cancel",
    response_class=HTMLResponse, dependencies=[],
)
async def web_node_edit_cancel(job_id: str, node_key: str):
    """Clear the inline edit slot (htmx swaps in an empty string)."""
    return HTMLResponse("")


@router.post("/jobs/{job_id}/nodes", response_class=HTMLResponse, dependencies=[])
async def web_node_insert(
    request: Request, job_id: str,
    node_key: str = Form(...), title: str = Form(...),
    tool: str = Form("LLM"), depends_on: str = Form(""),
    db=Depends(get_db),
):
    from app.modules import node_editor
    spec = {
        "node_key": node_key.strip(), "title": title.strip(), "tool": tool.strip() or "LLM",
        "depends_on": [d.strip() for d in depends_on.split(",") if d.strip()],
    }
    result = await node_editor.insert_node(job_id, spec, edited_by="web", db=db)
    if isinstance(result, dict) and "error" in result:
        logger.info("web_node_insert_noop job=%s key=%s err=%s",
                    job_id, node_key, result.get("error"))
    return await _node_action_response(request, job_id, db)


@router.post(
    "/jobs/{job_id}/nodes/{node_key}/move",
    response_class=HTMLResponse, dependencies=[],
)
async def web_node_move(
    request: Request, job_id: str, node_key: str,
    dir: str = "down", db=Depends(get_db),
):
    """Move a node up/down one slot by swapping it with its neighbour in the
    execution_order, then reorder."""
    from app.modules import node_editor
    nodes = await node_editor._load_nodes(db, job_id)
    keys = [n["node_key"] for n in nodes]
    if node_key in keys:
        i = keys.index(node_key)
        j = i - 1 if dir == "up" else i + 1
        if 0 <= j < len(keys):
            keys[i], keys[j] = keys[j], keys[i]
            await node_editor.reorder_nodes(job_id, keys, edited_by="web", db=db)
    return await _node_action_response(request, job_id, db)


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
    # §17.605 — html.escape does NOT encode newlines, but a raw \n/\r in an
    # operator-supplied node title/error truncates the SSE `data:` field (SSE
    # frames are newline-delimited), corrupting the fragment. Collapse CR/LF to
    # spaces on every escaped dynamic value; the static template parts below are
    # already single-line.
    def esc(s: str) -> str:
        return _html_lib.escape(s).replace("\r", " ").replace("\n", " ")

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
    if event_name == "progress":
        # §17.811 — live progress + ETA snapshot.
        summary = esc(str(data.get("summary", "")))
        current = data.get("current_item")
        current_html = (
            f' <span class="event-meta">now: {esc(str(current))}</span>'
            if current else ""
        )
        return (
            f'<li class="run-event run-event-progress">'
            f'<span class="event-icon">📊</span> {summary}{current_html}</li>'
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
    cause the generator to emit a distinct ``close`` SSE frame and stop.
    The browser ``<ul sse-close="close">`` listens for that frame and calls
    ``EventSource.close()`` — without it (§17.609) the EventSource treats the
    clean stream end as a dropped connection, auto-reconnects, re-POSTs
    ``/execute/all``, hits the completed-job guard, renders THAT as another
    terminal message, and loops forever appending error banners.

    §17.609 — heartbeats are forwarded as SSE comment lines (``: keep-alive``)
    so a long single node still produces frames and idle-timeout proxies don't
    tear the connection down (which would itself feed the reconnect loop).
    """

    async def _gen():
        try:
            async for evt in async_long_client.aiter_execute_all(
                job_id, include_heartbeats=True,
            ):
                event_name = evt.get("event") if isinstance(evt, dict) else None
                # Keepalive heartbeats surface as {"event": "heartbeat"}; forward
                # them as SSE comment lines (no visible fragment) to hold the
                # connection open behind idle-timeout proxies.
                if event_name == "heartbeat":
                    yield ": keep-alive\n\n"
                    continue
                data = evt.get("data") if isinstance(evt, dict) else {}
                if not isinstance(data, dict):
                    data = {"raw": str(data)}
                line = _render_event_html(event_name or "unknown", data)
                # Single-line data; SSE message format requires `data:`
                # prefix and trailing blank line per event.
                yield f"event: message\ndata: {line}\n\n"
                if event_name in _TERMINAL_EVENTS:
                    # Tell the browser to stop the EventSource — no reconnect.
                    yield "event: close\ndata: done\n\n"
                    return
        except Exception as exc:  # streaming failed mid-flight
            logger.exception(
                "web_run_stream_failed: job=%s error=%s", job_id, exc,
            )
            err_html = _render_event_html("error", {"message": str(exc)})
            yield f"event: message\ndata: {err_html}\n\n"
            # Close on the error path too, else the reconnect loop resumes.
            yield "event: close\ndata: done\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
