"""§17.820 (plan 5.9) — /web is retired: every route 301s to its /ui SPA home.

The server-rendered htmx console (Sprint J.2.a → §17.614) reached full parity
in the §17.780–818 SPA and is now permanent redirects only. The scenario
coverage its 1,593 lines of tests carried was ported to API/SPA tests first
(see tests/test_web_retirement.py, test_ideate_input_validation.py,
test_research_detached.py, test_job_phase.py, tests/ui/*) — several ports
exposed real API gaps the form was masking (blank-idea job rows, unknown
domain → 500, no detached-research or session-detail JSON endpoints), fixed
in the same change.

Deletion schedule (one release of redirects, then remove):
  - app/templates/web/* + app/templates/research_pdf_upload.html stays (the
    /research/pdf upload page lives in the research router, not here)
  - app/static/web.css, app/static/vendor/htmx-*.js
  - app/middleware/web_csrf.py + its app/main.py registration + tests
  - the "/web/" entry in app/auth.py _AUTH_EXEMPT_PREFIXES (redirects must
    stay reachable without a key so old bookmarks land on the SPA login)
  - this module + the app/main.py include

phase_label_for (imported by app/routers/status.py) moved to
app/modules/job_phase.py — do not resurrect imports from here.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

# include_in_schema=False — redirects are not API contract surface.
router = APIRouter(prefix="/web", tags=["web-ui"], include_in_schema=False)

_MOVED = 301


def _ui(hash_path: str) -> RedirectResponse:
    """301 to an SPA hash route. The fragment survives the redirect — browsers
    apply the Location header's #… verbatim."""
    return RedirectResponse(f"/ui/#{hash_path}", status_code=_MOVED)


@router.get("/jobs")
def web_jobs_redirect():
    return _ui("/")


@router.get("/jobs/{job_id}")
@router.get("/jobs/{job_id}/fragment")
def web_job_detail_redirect(job_id: str):
    return _ui(f"/theater/{job_id}")


@router.get("/new")
def web_new_redirect():
    return _ui("/new")


@router.get("/rag")
def web_rag_redirect():
    return _ui("/rag")


@router.get("/model")
def web_model_redirect():
    return _ui("/models")


@router.get("/research")
def web_research_redirect():
    return _ui("/research")


@router.get("/research/{session_id}")
@router.get("/research/{session_id}/fragment")
def web_research_detail_redirect(session_id: str):
    return _ui(f"/research/{session_id}")


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
def web_catchall_redirect(path: str):
    """Anything else — old POST form actions, SSE streams, node-action URLs —
    lands on the SPA root. 301 downgrades a follow-up POST to GET in every
    real client, which is exactly right: the write surfaces moved to the
    authenticated JSON API (/ideate/start, /ideate/confirm, /nodes/*,
    /models/roles/*, /research/start)."""
    return _ui("/")
