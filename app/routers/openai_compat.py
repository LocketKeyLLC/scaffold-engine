"""Native OpenAI-compatible surface — ``/v1/*`` (§17.788).

Mounted as a **sub-app** in ``app/main.py`` (``app.mount("/v1", openai_app)``), so
it bypasses the parent app's global ``Depends(require_api_key)`` and carries its
own :func:`app.auth.require_openai_key` guard (Bearer OR X-API-Key). Errors are
rendered as the OpenAI ``{"error": {...}}`` envelope that stock OpenAI SDKs
expect. Mounting (rather than ``include_router``) also keeps these routes out of
the main OpenAPI snapshot — the OpenAI contract is a separate surface.

Phase 0 (this file): the wire protocol end-to-end. ``POST /v1/chat/completions``
is a thin passthrough to ``model_router`` (role ``model_general``), streaming or
not; ``GET /v1/models`` advertises the single ``scaffold-engine`` model. The
native dispatcher (triage, NL routing, /go auto-chain) replaces the passthrough
in later phases — see ``docs/native_openai_surface_plan.md``.
"""
from __future__ import annotations

import json
import logging

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import model_router, openai_schemas as oai
from app.auth import require_openai_key
from app.config import settings

logger = logging.getLogger("scaffold")

# Sub-app: docs/openapi disabled — this surface is the OpenAI contract, not ours.
openai_app = FastAPI(
    title="Scaffold Engine — OpenAI-compatible surface",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_SSE_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}


def _error_envelope(status_code: int, message: str, err_type: str, code: str | None) -> JSONResponse:
    """Render an OpenAI-shaped error body: ``{"error": {message, type, code}}``."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


@openai_app.exception_handler(StarletteHTTPException)
async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
    err_type = "invalid_request_error" if exc.status_code < 500 else "api_error"
    if exc.status_code == 401:
        err_type = "authentication_error"
    return _error_envelope(exc.status_code, str(exc.detail), err_type, None)


@openai_app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    return _error_envelope(400, str(exc.errors()), "invalid_request_error", None)


@openai_app.get("/models")
async def list_models(_: str = Depends(require_openai_key)) -> oai.ModelList:
    """Advertise the engine as a single OpenAI-selectable model."""
    return oai.model_list(settings.native_openai_model_id)


@openai_app.post("/chat/completions")
async def chat_completions(
    body: oai.ChatCompletionRequest,
    _: str = Depends(require_openai_key),
):
    """OpenAI ``chat.completions``. Phase-0 passthrough to ``model_general``.

    Honors ``stream``: a ``StreamingResponse`` of ``chat.completion.chunk`` SSE
    frames when true, a single ``chat.completion`` object when false.
    """
    router_messages = oai.to_router_messages(body.messages)
    temperature = body.resolved_temperature(0.7)
    max_tokens = body.resolved_max_tokens(4096)
    completion_id = oai.new_completion_id()
    model_id = body.model or settings.native_openai_model_id

    if body.stream:
        return StreamingResponse(
            _stream_passthrough(
                router_messages, temperature, max_tokens, completion_id, model_id
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    resp = await model_router.chat(
        router_messages,
        role="model_general",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not resp.success:
        return _error_envelope(
            502, resp.error or "generation failed", "api_error", "generation_failed"
        )
    return oai.completion_response(
        completion_id=completion_id,
        model=model_id,
        content=resp.text,
        prompt_tokens=resp.tokens_prompt,
        completion_tokens=resp.tokens_completion,
    )


async def _stream_passthrough(messages, temperature, max_tokens, completion_id, model_id):
    """Yield OpenAI ``data: {chunk}\\n\\n`` SSE frames for a passthrough stream.

    Frame 1 is the assistant role delta; then content deltas from
    ``model_router.stream_chat``; then a terminal ``finish_reason:"stop"`` chunk
    and ``data: [DONE]``. A mid-stream provider error is surfaced as a final
    content delta rather than a broken connection (status is already committed).
    """
    def _frame(delta, finish_reason=None) -> str:
        return "data: " + json.dumps(
            oai.chunk(
                completion_id=completion_id,
                model=model_id,
                delta=delta,
                finish_reason=finish_reason,
            )
        ) + "\n\n"

    yield _frame({"role": "assistant"})
    try:
        async for piece in model_router.stream_chat(
            messages, role="model_general", temperature=temperature, max_tokens=max_tokens
        ):
            if piece:
                yield _frame({"content": piece})
    except Exception as exc:  # pragma: no cover - defensive; status already sent
        logger.warning('event="openai_stream_error" error=%s', exc)
        yield _frame({"content": f"\n\n[stream error: {exc}]"})
    yield _frame({}, finish_reason="stop")
    yield "data: [DONE]\n\n"
