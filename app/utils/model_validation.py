"""§17.174 — extracted from ``app/main.py`` so per-domain routers can
import it without circular-import risk.

``_require_valid_models`` was previously a module-private helper in
``app/main.py``. The §17.174 router refactor splits endpoints out of
main.py into ``app/routers/{workflow,research,jobs,schedule,gt,prompts,rag}.py``
and each router needs to call this gate before invoking
model-using endpoints. Extracting once here is cleaner than
importing the function from main.py (which would re-trigger main's
top-of-module side effects — middleware registration etc. — every
time a router module loads).

Public name kept the same (``_require_valid_models``) and routers
import via ``from app.utils.model_validation import _require_valid_models``
so a future reader greps the same identifier across the codebase.
"""
from fastapi import HTTPException

from app.model_router import validate_models


async def _require_valid_models(overrides: dict | None = None):
    """Raise 503 if Ollama unreachable, 422 if models missing.

    The two failure modes are distinct enough that clients want
    different responses: 503 retryable (Ollama restarting), 422 not
    (model genuinely not pulled). Returning None indicates Ollama is
    unreachable; an empty list indicates Ollama is reachable but
    declares no missing models (all good); a non-empty list is
    the set of missing role→model names.
    """
    missing = await validate_models(overrides)
    if missing is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "ollama_unreachable",
                "hint": "Check Ollama with: curl http://localhost:11434/api/tags",
            },
        )
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "model_validation_failed",
                "missing_models": missing,
                "hint": "Check Ollama with: curl http://localhost:11434/api/tags",
            },
        )
