"""Scaffold Engine — Model router.

Ollama dispatch with:
  - Timeout handling (cloud vs local)
  - Retry cascade (3x same model → fallback swap)
  - Performance metric capture (for Step 9 middleware)

Step 7 of 23-step build plan.

Sprint E refactor: ``ModelResponse`` is now defined in
``app.providers.base`` and re-exported here so existing imports keep
working. Concrete provider classes live in ``app.providers.*``;
``OllamaProvider`` delegates back into this module so tests that
``patch.object(model_router, "_call_ollama", ...)`` keep intercepting
all dispatch paths — including those reached through the provider
abstraction. The provider abstraction is what later sprints (E.7+)
use to route per-role calls to non-Ollama backends.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any, Optional

import httpx

from app.config import settings
from app.providers.base import ModelResponse, Tool, ToolCall  # noqa: F401 — public re-export
from app.utils.llm_parsing import parse_json_array, parse_json_object
from app.utils.tool_call_args import read_tool_args

logger = logging.getLogger("scaffold.router")

# ---------------------------------------------------------------------------
# Ollama HTTP client — delegates to the shared pool in app.utils.http_clients
# (initialized eagerly at app lifespan startup; closed by close_clients()).
# ---------------------------------------------------------------------------


def _get_client() -> httpx.AsyncClient:
    """Return the shared Ollama AsyncClient.

    Kept as a thin wrapper rather than importing ``get_ollama_client``
    everywhere so existing tests that ``patch.object(model_router,
    "_get_client", return_value=...)`` continue to work without
    modification.
    """
    from app.utils.http_clients import get_ollama_client
    return get_ollama_client()


async def close_client() -> None:
    """No-op retained for backward compatibility.

    The Ollama client is owned by ``app.utils.http_clients`` and is
    closed by its ``close_clients()`` shutdown hook. This function is
    preserved so existing callers in main.py's lifespan don't break;
    new code should rely on ``close_clients()`` directly.
    """
    return None

# ---------------------------------------------------------------------------
# Cloud model detection
# ---------------------------------------------------------------------------

CLOUD_MODELS = frozenset({
    settings.model_cloud_heavy,
    settings.model_cloud_alt,
})


def _is_cloud(model: str) -> bool:
    # §17.596 — Ollama cloud tags use BOTH suffix forms: "-cloud"
    # (qwen3.5:397b-cloud) and ":cloud" (kimi-k2.7-code:cloud,
    # qwen3-coder-next:cloud, the shipped coder/verifier/extract roles). Match
    # both so cloud roles get cloud_timeout instead of the ~30-min local_timeout
    # that aborts genuinely slow cloud calls early.
    return (
        model in CLOUD_MODELS
        or model.endswith("-cloud")
        or model.endswith(":cloud")
    )

def _timeout_for(model: str) -> int:
    return settings.cloud_timeout if _is_cloud(model) else settings.local_timeout


def _model_lacks_native_tools(model: str) -> bool:
    """§17.547 — True if ``model`` is known not to emit native ``tool_calls``.

    Ollama's ``supports_native_tools`` flag is provider-wide, but tool-call
    support is really per-model: qwen3.5 thinking models return their answer in
    content/thinking and never populate ``message.tool_calls``, yielding a 100%
    tool-call miss. ``tool_call`` routes these through the JSON-coaxing fallback
    instead. Substring match (case-insensitive) on ``settings.tool_call_coax_models``.
    """
    m = (model or "").lower()
    return any(sub.lower() in m for sub in settings.tool_call_coax_models)


def _smart_fallback(model: str, default_fallback: str) -> str:
    """Map non-existent models to appropriate fallbacks."""
    model_lower = model.lower()
    # Code-related models → coder
    if any(x in model_lower for x in ["code", "coder", "codegen"]):
        return settings.model_coder
    return default_fallback


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------

async def _call_ollama(
    endpoint: str,
    payload: dict[str, Any],
    model: str,
    timeout: int,
) -> ModelResponse:
    """Single Ollama API call. Returns ModelResponse regardless of outcome."""
    url = f"{settings.ollama_base_url}{endpoint}"
    start = time.monotonic()
    try:
        client = _get_client()
        resp = await client.post(url, json=payload, timeout=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if resp.status_code != 200:
            return ModelResponse(
                model=model,
                success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                total_duration_ms=elapsed_ms,
                provider="ollama",
            )

        data = resp.json()
        # Extract text from the response format
        text = (
            data.get("response")            # /api/generate
            or data.get("message", {}).get("content", "")  # /api/chat
            or ""
        )

        # Extract performance metrics from Ollama response
        tokens_prompt = data.get("prompt_eval_count")
        tokens_completion = data.get("eval_count")
        # Ollama reports durations in nanoseconds
        ttft_ns = data.get("prompt_eval_duration")
        ttft_ms = int(ttft_ns / 1_000_000) if ttft_ns else None
        eval_duration_ns = data.get("eval_duration")
        tps = None
        if tokens_completion and eval_duration_ns and eval_duration_ns > 0:
            tps = round(tokens_completion / (eval_duration_ns / 1e9), 2)

        return ModelResponse(
            text=text.strip(),
            model=model,
            success=True,
            ttft_ms=ttft_ms,
            total_duration_ms=elapsed_ms,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_per_sec=tps,
            provider="ollama",
            raw=data,
            )

    except httpx.TimeoutException:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ModelResponse(
            model=model,
            success=False,
            error=f"Timeout after {timeout}s",
            total_duration_ms=elapsed_ms,
            provider="ollama",
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return ModelResponse(
            model=model,
            success=False,
            error=str(e),
            total_duration_ms=elapsed_ms,
            provider="ollama",
        )


# ---------------------------------------------------------------------------
# Retry + fallback wrapper
# ---------------------------------------------------------------------------

# HTTP status codes worth retrying. Everything else (auth, validation, 404)
# won't recover by waiting — bail to fallback immediately.
# 529 = Anthropic "overloaded_error" (returned on hosted-API brownouts); it is
# transient and retryable just like 429/5xx (§17.610).
_RETRYABLE_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

# Extracts the HTTP status from an error string. Matches both the Ollama path's
# "HTTP 429: ..." and the provider-prefixed "openai HTTP 429: ..." /
# "anthropic HTTP 529: ..." forms (§17.610), so cloud errors classify correctly.
_HTTP_CODE_RE = re.compile(r"HTTP (\d{3})")

# Full-jitter exponential backoff: sleep ∈ [0, min(BASE * 2^attempt, CAP)].
# Base 0.5s / cap 8s gives windows of [0,0.5], [0,1], [0,2], [0,4], [0,8] —
# enough to ride out an Ollama brownout without piling on, and the 0-floor
# decorrelates concurrent retries (AWS "full jitter" pattern).
_BACKOFF_BASE_SEC = 0.5
_BACKOFF_CAP_SEC = 8.0


def _classify_failure(resp: ModelResponse) -> str:
    """Return ``'retry'`` for transient failures, ``'fail_fast'`` for
    deterministic ones.

    Transient: timeouts, 5xx, 429, connection-class exceptions (RST,
    ECONNREFUSED, DNS) — these can recover on a second attempt.
    Deterministic: 4xx other than 408/425/429 — auth, validation, missing
    model. Retrying the same primary just delays the inevitable fallback.
    """
    err = resp.error or ""
    if err.startswith("Timeout"):
        return "retry"
    m = _HTTP_CODE_RE.search(err)
    if m:
        code = int(m.group(1))
        return "retry" if code in _RETRYABLE_HTTP_CODES else "fail_fast"
    # Generic exception path (httpx connect errors, RST, DNS, etc.) — and
    # unparseable HTTP strings — are almost always transient, so retry.
    return "retry"


def _backoff_seconds(attempt: int) -> float:
    """Full-jitter exponential backoff. ``attempt`` is 0-indexed: 0 is the
    delay *after* the first failed call, before the second call."""
    capped = min(_BACKOFF_BASE_SEC * (2 ** attempt), _BACKOFF_CAP_SEC)
    return random.uniform(0.0, capped)


async def _sleep_for_attempt(attempt: int) -> None:
    """Coroutine wrapper around the backoff sleep so tests can patch it."""
    await asyncio.sleep(_backoff_seconds(attempt))


async def _record_call(resp: ModelResponse) -> ModelResponse:
    """Sprint J.3.a — fire-and-forget cost/latency telemetry hook.

    Imports the recorder lazily and never lets a failure break the LLM
    call path. Returns the input untouched so callers can inline this as
    ``return await _record_call(resp)``. The recorder reads job/node
    ContextVars set by ``execute_next_node`` and writes one
    ``llm_call_logs`` row.

    §17.163 — ``record_llm_call`` promises not to raise (three internal
    try/except layers around the import, the DB write, and the metrics
    emit). Under normal operation neither except path here fires; if
    one does, that's a contract bug in cost_tracking worth
    investigating, not silent telemetry loss. The two failure modes
    are split + logged so the journal distinguishes "deployment
    missing cost_tracking" from "cost_tracking raised despite its
    contract."
    """
    try:
        from app.utils.cost_tracking import record_llm_call
    except ImportError:
        logger.warning("record_call_import_failed: cost_tracking unavailable")
        return resp
    try:
        await record_llm_call(resp)
    except Exception:
        logger.exception("record_call_unexpected_escape")
    # §17.435 — emit a gen_ai.* OTel span for LLM observability (Phoenix).
    # No-op unless OTel is initialized; guarded so it never breaks the call.
    try:
        from app.observability.llm_spans import record_llm_span
        record_llm_span(resp)
    except Exception:
        logger.debug("record_llm_span_escape", exc_info=True)
    # §17.786 — capture the full request/response CONTENT into llm_traces.
    # No-op unless settings.trace_capture_enabled is on; reads the request
    # snapshot set by _begin_trace at the public entry point. Guarded so a
    # trace-write failure never breaks the LLM call path.
    try:
        from app.utils.trace_capture import record_trace
        await record_trace(resp)
    except Exception:
        logger.debug("record_trace_escape", exc_info=True)
    return resp


def _begin_trace(
    kind: str,
    *,
    prompt: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    system: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> None:
    """§17.786 — stash the in-flight request for the trace writer.

    Called at the top of each public entry point (generate/chat/tool_call/
    embed). The resolved model/provider/tokens are read from the returned
    ``ModelResponse`` at write time, so this only needs the request content +
    sampling params known here. No-op (and cheap) when trace capture is off;
    never raises."""
    try:
        from app.utils.trace_capture import set_current_request
        set_current_request(
            kind, prompt=prompt, messages=messages, system=system,
            temperature=temperature, max_tokens=max_tokens,
        )
    except Exception:
        logger.debug("begin_trace_escape", exc_info=True)


async def _dispatch_with_retry(
    endpoint: str,
    payload: dict[str, Any],
    model: str,
    fallback: str | None = None,
    max_retries: int | None = None,
) -> ModelResponse:
    """Retry cascade with exponential backoff + per-error-class branching.

    Transient failures (timeout, 5xx, 429, connection-class) are retried on
    the primary up to ``max_retries`` times, sleeping with full-jitter
    exponential backoff between attempts. Deterministic failures (4xx
    other than 408/425/429) skip remaining primary attempts and jump
    straight to the fallback — waiting won't fix a 401 or a missing model.
    """
    retries = max_retries if max_retries is not None else settings.max_retries
    # §16.7 — /api/embed has no valid fallback: the embedder is config-only
    # (dim-locked at 512) and the global ``model_fallback`` is a chat model that
    # returns HTTP 501 on /api/embed, so injecting it just burns a doomed
    # round-trip. Honor the embed callers' explicit ``fallback=None`` instead.
    # Chat/generate/classify keep the smart-fallback default unchanged.
    if endpoint != "/api/embed":
        fallback = fallback or _smart_fallback(model, settings.model_fallback)

    # §17.409 (arch-review R4) — shallow-copy so the per-attempt/fallback
    # ``payload["model"] = …`` swaps below never mutate the caller's dict.
    payload = dict(payload)

    # Phase 1: retry primary model
    last_resp = ModelResponse(model=model, success=False, error="no attempt", provider="ollama")
    attempts_used = 0
    for attempt in range(retries):
        attempts_used = attempt + 1
        payload["model"] = model
        last_resp = await _call_ollama(
            endpoint, payload, model, _timeout_for(model),
        )
        if last_resp.success:
            last_resp.retries = attempt
            return last_resp

        classification = _classify_failure(last_resp)
        logger.warning(
            "Attempt %d/%d failed for %s [%s]: %s",
            attempt + 1, retries, model, classification, last_resp.error,
        )
        if classification == "fail_fast":
            break
        # Sleep before the next primary attempt only; no point sleeping
        # after the last attempt or right before the fallback swap.
        if attempt + 1 < retries:
            await _sleep_for_attempt(attempt)

    # Phase 2: fallback (skip if fallback == primary)
    if fallback and fallback != model:
        logger.info("Falling back from %s → %s", model, fallback)
        payload["model"] = fallback
        fb_resp = await _call_ollama(
            endpoint, payload, fallback, _timeout_for(fallback),
        )
        fb_resp.retries = attempts_used
        fb_resp.fallback_used = True
        if fb_resp.success:
            return fb_resp
        last_resp = fb_resp

    # All attempts exhausted
    last_resp.retries = attempts_used
    logger.error("All attempts exhausted for %s (fallback: %s)", model, fallback)
    return last_resp


async def _retry_provider_call(
    call: "Callable[[], Any]",  # () -> Awaitable[ModelResponse]  # noqa: F821
    *,
    model: str,
    max_retries: int | None = None,
) -> ModelResponse:
    """§17.610 — provider-agnostic retry cascade for the role-routed path.

    The legacy Ollama path gets its retry/backoff from ``_dispatch_with_retry``,
    but role-routed cloud calls (openai/anthropic) previously issued a single
    provider POST with no retry — so a transient 429/5xx/529 hard-failed the
    whole LLM call. Hosted APIs throttle MORE than a local Ollama yet had the
    LEAST resilience.

    This wraps any coroutine returning a ``ModelResponse`` in the same
    full-jitter backoff + ``_classify_failure`` branching. It does NOT do the
    ``_dispatch_with_retry`` model-swap fallback: the provider owns its own
    ``fallback`` kwarg semantics, and swapping models across providers is out
    of scope here.
    """
    retries = max_retries if max_retries is not None else settings.max_retries
    retries = max(1, retries)
    last_resp = ModelResponse(model=model, success=False, error="no attempt")
    for attempt in range(retries):
        last_resp = await call()
        if last_resp.success:
            last_resp.retries = attempt
            return last_resp
        classification = _classify_failure(last_resp)
        logger.warning(
            "Provider attempt %d/%d failed for %s [%s]: %s",
            attempt + 1, retries, model, classification, last_resp.error,
        )
        if classification == "fail_fast":
            break
        if attempt + 1 < retries:
            await _sleep_for_attempt(attempt)
    last_resp.retries = retries - 1
    return last_resp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _resolve_role(role: str, overrides: dict | None) -> tuple[str, "LLMProvider"]:  # noqa: F821
    """Resolve (model_tag, provider_singleton) for a role.

    Sprint E.7 seam: imported lazily so the model_router module stays
    importable even if app.providers fails to initialize (e.g., a third-
    party SDK is missing). Lazy import also avoids the circular-import
    problem — app.providers.ollama imports model_router.
    """
    from app.config import get_model
    from app.providers import provider_for_role
    return get_model(role, overrides), provider_for_role(role, overrides)


def _reject_role_model_collision(role: str | None, model: str | None) -> None:
    if role and model:
        raise ValueError(
            "pass either role= (provider-routed) or model= (legacy direct), "
            "not both"
        )


def _format_provider_error(resp: ModelResponse, role: str) -> str:
    """Sprint G.1 — enrich a failed ModelResponse with role + provider
    context + a remediation hint matching the failure shape.

    The enriched string format is::

        [role=<role> provider=<provider>] <original error> — <hint>

    so that downstream consumers (job error_summary, OWUI SSE, logs) all
    inherit the same actionable text. Patterns recognized: 401 / 403 /
    404 / 429 / Timeout. Anything else just gets the structured prefix
    so users still see which role/provider failed.
    """
    base = (resp.error or "unknown error").strip()
    provider = resp.provider or "unknown"
    role_env = role.upper()

    hint = ""
    lower = base.lower()
    if "401" in base or "unauthorized" in lower:
        if provider == "openai":
            hint = (
                " — OPENAI_API_KEY rejected. Rotate at "
                "https://platform.openai.com/api-keys, update .env, "
                "and run 'make doctor' to verify."
            )
        elif provider == "ollama":
            hint = " — Ollama doesn't use auth; check OLLAMA_BASE_URL is reachable."
        else:
            hint = f" — rotate the {provider} API key in .env."
    elif "403" in base or "forbidden" in lower:
        hint = (
            f" — key lacks access. Check {provider} account permissions "
            f"and OPENAI_BASE_URL if pointing at a custom endpoint."
        )
    elif "404" in base or "not found" in lower or "no such model" in lower:
        hint = (
            f" — model unavailable on this provider. Set MODEL_{role_env} "
            f"in .env to a tag the provider serves "
            f"(see provider's /models endpoint)."
        )
    elif "429" in base or "rate limit" in lower or "quota" in lower:
        hint = (
            f" — {provider} rate-limit or quota exceeded. Back off, "
            f"or switch MODEL_{role_env}_PROVIDER to a different backend."
        )
    elif "529" in base or "overloaded" in lower:
        hint = (
            f" — {provider} is temporarily overloaded (retried with backoff). "
            f"Retry shortly, or switch MODEL_{role_env}_PROVIDER to a different "
            f"backend if it persists."
        )
    elif "timeout" in lower:
        if provider == "openai":
            hint = " — call exceeded OPENAI_TIMEOUT. Raise it in .env (or shrink the prompt)."
        elif provider == "ollama":
            hint = (
                " — Ollama call exceeded its timeout. Raise CLOUD_TIMEOUT "
                "(for cloud-suffixed models) or LOCAL_TIMEOUT in .env."
            )
        else:
            hint = f" — {provider} call timed out. Raise the provider's timeout setting."

    return f"[role={role} provider={provider}] {base}{hint}"


def _effective_response_schema(
    response_schema: dict | str | None,
    provider,
) -> dict | str | None:
    """§17.773 — PROVIDER-AWARE valve gate for grammar-constrained decoding.

    Returns ``response_schema`` only when BOTH the master valve
    (``structured_outputs_enabled``) is on AND ``provider`` actually enforces the
    constraint; otherwise ``None`` so the dispatch path drops it and falls back to
    the json_repair parse — byte-identical to the pre-§17.773 behavior for that
    call. This is the single chokepoint; providers trust that a non-None schema
    means "the operator enabled this AND you enforce it".

    Enforcement is read from ``provider.supports_structured_outputs``
    (True for OpenAI/Anthropic). Ollama is False by default because the
    cloud-proxied models this engine runs ignore ``format`` (live smoke), but a
    local-model deployment re-enables it via ``structured_outputs_ollama_enabled``
    — so turning the master valve on applies the constraint ONLY where it bites.
    """
    if not response_schema or not settings.structured_outputs_enabled:
        return None
    supported = getattr(provider, "supports_structured_outputs", False)
    if (
        not supported
        and getattr(provider, "name", "") == "ollama"
        and settings.structured_outputs_ollama_enabled
    ):
        supported = True
    return response_schema if supported else None


async def generate(
    prompt: str,
    model: str | None = None,
    *,
    role: str | None = None,
    overrides: dict | None = None,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    fallback: str | None = None,
    think: bool | None = None,
    response_schema: dict | str | None = None,
) -> ModelResponse:
    """Generate text. ``role=`` routes via the provider abstraction.

    Pass ``role="model_general"`` (etc.) to dispatch through whichever
    provider is bound to that role in settings/overrides. Pass ``model=``
    for the legacy direct-Ollama path. The two are mutually exclusive.

    §17.683 — ``think=False`` disables a reasoning model's chain-of-thought so
    the whole num_predict budget goes to the answer (the ``thinking`` field is
    discarded on this path anyway). ``None`` leaves the model default untouched.
    Only the Ollama provider honors it; other providers ignore it via ``**opts``.

    §17.773 — ``response_schema`` (a JSON Schema ``dict`` or the string ``"json"``)
    requests grammar-constrained decoding: the backend constrains generation to
    schema-valid JSON so callers don't need post-hoc json_repair. Applied only
    when ``settings.structured_outputs_enabled`` is on AND the resolved provider
    enforces it (OpenAI/Anthropic; Ollama via ``structured_outputs_ollama_enabled``)
    — otherwise dropped, byte-identical to the legacy path. See
    ``_effective_response_schema``.
    """
    _reject_role_model_collision(role, model)
    _begin_trace(
        "generate", prompt=prompt, system=system,
        temperature=temperature, max_tokens=max_tokens,
    )
    if role:
        resolved_model, provider = _resolve_role(role, overrides)
        schema = _effective_response_schema(response_schema, provider)
        resp = await _retry_provider_call(
            lambda: provider.generate(
                resolved_model, prompt,
                system=system, temperature=temperature, max_tokens=max_tokens,
                fallback=fallback, think=think, response_schema=schema,
            ),
            model=resolved_model,
        )
        if not resp.success:
            resp.error = _format_provider_error(resp, role)
        return await _record_call(resp)

    # Legacy direct path is always Ollama — gate against the ollama provider.
    from app.providers import get_provider
    schema = _effective_response_schema(response_schema, get_provider("ollama"))
    model = model or settings.model_general
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if system:
        payload["system"] = system
    if think is not None:
        payload["think"] = think
    if schema:
        payload["format"] = schema
    resp = await _dispatch_with_retry("/api/generate", payload, model, fallback)
    return await _record_call(resp)


async def generate_json(
    prompt: str,
    schema: dict,
    *,
    model: str | None = None,
    role: str | None = None,
    overrides: dict | None = None,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    fallback: str | None = None,
    think: bool | None = None,
    as_array: bool = False,
) -> tuple[Any, ModelResponse]:
    """§17.773 — generate JSON with grammar-constrained decoding, then parse.

    The single entry point that replaces the ``generate(...) + parse_json_object``
    pattern at call sites. It requests constrained decoding for ``schema`` (gated
    by ``settings.structured_outputs_enabled``) and parses the result through the
    shared ``llm_parsing`` chain — ``json.loads`` first, then the json_repair
    fallback. That fallback is why nothing regresses when the valve is off or a
    model ignores the grammar: the output still parses when it can.

    Returns ``(parsed, resp)``. ``parsed`` is the ``dict`` (or ``list`` when
    ``as_array=True``) or ``None`` on empty/unparseable output; ``resp`` is the
    full :class:`ModelResponse` so callers keep telemetry, error text, and the
    redraw-on-empty signal they had with the two-call pattern.
    """
    resp = await generate(
        prompt, model=model, role=role, overrides=overrides,
        system=system, temperature=temperature, max_tokens=max_tokens,
        fallback=fallback, think=think, response_schema=schema,
    )
    if not resp.success:
        return None, resp
    text = resp.text or ""
    parsed = parse_json_array(text) if as_array else parse_json_object(text)
    return parsed, resp


async def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    *,
    role: str | None = None,
    overrides: dict | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    fallback: str | None = None,
    response_schema: dict | str | None = None,
) -> ModelResponse:
    """Chat completion. ``role=`` routes via the provider abstraction.

    §17.773 — ``response_schema`` requests grammar-constrained decoding; see
    ``generate`` for semantics. Provider-aware gate: applied only when the master
    valve is on AND the resolved provider enforces schemas.
    """
    _reject_role_model_collision(role, model)
    _begin_trace(
        "chat", messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )
    if role:
        resolved_model, provider = _resolve_role(role, overrides)
        schema = _effective_response_schema(response_schema, provider)
        resp = await _retry_provider_call(
            lambda: provider.chat_completion(
                resolved_model, messages,
                temperature=temperature, max_tokens=max_tokens,
                fallback=fallback, response_schema=schema,
            ),
            model=resolved_model,
        )
        if not resp.success:
            resp.error = _format_provider_error(resp, role)
        return await _record_call(resp)

    # Legacy direct path is always Ollama — gate against the ollama provider.
    from app.providers import get_provider
    schema = _effective_response_schema(response_schema, get_provider("ollama"))
    model = model or settings.model_general
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if schema:
        payload["format"] = schema
    resp = await _dispatch_with_retry("/api/chat", payload, model, fallback)
    return await _record_call(resp)


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    role: str | None = None,
    overrides: dict | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
):
    """Role-routed streaming chat — yields content-delta ``str`` chunks.

    Delegates to the resolved provider's ``stream_chat`` (the unified Sprint I.1
    contract: content-only, reasoning/thinking deltas filtered). First real
    consumer of that path. Pass ``role=`` (provider-routed, the assist path) or
    ``model=`` (legacy direct). NOTE: unlike ``chat``, the stream path does NOT
    ``_record_call`` (token usage isn't available mid-stream); callers that need
    cost tracking use the non-stream ``chat`` fallback.
    """
    _reject_role_model_collision(role, model)
    if role:
        resolved_model, provider = _resolve_role(role, overrides)
    else:
        from app.providers import get_provider
        resolved_model = model or settings.model_general
        provider = get_provider(settings.model_general_provider)
    async for chunk in provider.stream_chat(
        resolved_model, messages, temperature=temperature, max_tokens=max_tokens,
    ):
        yield chunk


async def tool_call(
    messages: list[dict[str, str]],
    tools: list[Tool],
    model: str | None = None,
    *,
    role: str | None = None,
    overrides: dict | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tool_choice: str = "auto",
    fallback: str | None = None,
    draws: int = 3,
) -> ModelResponse:
    """Call an LLM with native tool-calling, falling back to JSON-coaxing
    for providers that don't support native tools.

    Sprint W.6 — single public entry point so callers can replace
    ``model_router.chat() + parse_json_object()`` patterns with one call
    that returns structured arguments via ``resp.tool_calls[0].arguments``.

    Behavior:
      - role=...: dispatched through the provider abstraction.
        ``supports_native_tools=True`` → provider.tool_call().
        Otherwise → coaxing fallback (chat + JSON parse) so role-bound
        callers keep working on mixed-capability stacks.
      - model=...: legacy direct path. Goes through the registered
        ``ollama`` provider (matches generate/chat behavior).
      - Empty ``tools`` list short-circuits to chat (returns a normal
        ModelResponse with empty tool_calls). Useful as a no-op test path.

    Returns a :class:`ModelResponse`. On success with a tool invocation,
    ``tool_calls`` is populated. On the coaxing fallback, the wrapper
    synthesizes a single ToolCall (``id="coaxed_0"``) by parsing the
    response against the *first* tool's ``input_schema``. On parse
    failure, ``tool_calls`` stays empty — callers treat that as "no
    tool selected" or a soft failure.

    §17.583 — built-in retry-on-empty-args. A thinking model
    (``qwen3.5:397b-cloud``) can return ``success=True`` yet no usable tool
    arguments (reasoning eats the budget; the coax parse finds no JSON). This
    re-draws up to ``draws`` times on that variance so EVERY caller reading via
    ``read_tool_args`` gets it uniformly — replacing the per-call-site
    ``llm_retry.tool_call_until_args`` wrapper. Hard failures
    (``success=False``) and the empty-``tools`` no-op path return immediately;
    pass ``draws=1`` to opt out (e.g. a router-style call where "no tool" is a
    valid answer).
    """
    resp = None
    attempts = max(1, draws)
    for d in range(attempts):
        resp = await _tool_call_once(
            messages, tools, model,
            role=role, overrides=overrides,
            temperature=temperature, max_tokens=max_tokens,
            tool_choice=tool_choice, fallback=fallback,
        )
        # Only re-draw the "success but no usable tool args" variance. Hard
        # failures and the empty-tools short-circuit are returned as-is.
        if not tools or not resp.success or read_tool_args(resp) is not None:
            return resp
        if d + 1 < attempts:
            logger.warning(
                "tool_call_empty_redraw: model/role=%s draw=%d/%d (no tool args, §17.583)",
                role or model or settings.model_general, d + 1, attempts,
            )
    return resp


async def _tool_call_once(
    messages: list[dict[str, str]],
    tools: list[Tool],
    model: str | None = None,
    *,
    role: str | None = None,
    overrides: dict | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tool_choice: str = "auto",
    fallback: str | None = None,
) -> ModelResponse:
    """One tool-call draw (native-first-then-coax + telemetry). See ``tool_call``
    for the retry wrapper (§17.583)."""
    _reject_role_model_collision(role, model)
    _begin_trace(
        "tool_call", messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )

    if role:
        resolved_model, provider = _resolve_role(role, overrides)
        if (
            getattr(provider, "supports_native_tools", False)
            and not _model_lacks_native_tools(resolved_model)
        ):
            resp = await _native_first_then_coax(
                provider, resolved_model, messages, tools,
                temperature=temperature, max_tokens=max_tokens,
                tool_choice=tool_choice, role=role, fallback=fallback,
            )
            return await _record_call(resp)
        coaxed = await _tool_call_via_coaxing(
            provider, resolved_model, messages, tools,
            temperature=temperature, max_tokens=max_tokens,
            role=role, fallback=fallback,
        )
        return await _record_call(coaxed)

    # Legacy direct-model path → go through the ollama provider's tool_call.
    model = model or settings.model_general
    from app.providers import get_provider
    provider = get_provider("ollama")
    if (
        getattr(provider, "supports_native_tools", False)
        and not _model_lacks_native_tools(model)
    ):
        resp = await _native_first_then_coax(
            provider, model, messages, tools,
            temperature=temperature, max_tokens=max_tokens,
            tool_choice=tool_choice, role=None, fallback=fallback,
        )
        return await _record_call(resp)
    coaxed = await _tool_call_via_coaxing(
        provider, model, messages, tools,
        temperature=temperature, max_tokens=max_tokens,
        role=None, fallback=fallback,
    )
    return await _record_call(coaxed)


async def _native_first_then_coax(
    provider,
    model: str,
    messages: list[dict[str, str]],
    tools: list[Tool],
    *,
    temperature: float,
    max_tokens: int,
    tool_choice: str,
    role: str | None,
    fallback: str | None,
) -> ModelResponse:
    """§17.548 — native-first tool call with a coaxing fallback.

    Tries the provider's native ``tool_call``. If the model succeeds but emits
    NO ``tool_calls`` (it answered in prose — common when the prompt doesn't
    compel the tool, and this Ollama ignores ``tool_choice`` so we can't force
    it), fall back to the JSON-coaxing path and parse the structured output
    from content. Tool-capable models that DO call the tool keep the clean,
    single-call native path; everything else still gets structured output.
    """
    resp = await _retry_provider_call(
        lambda: provider.tool_call(
            model, messages, tools,
            temperature=temperature, max_tokens=max_tokens, tool_choice=tool_choice,
        ),
        model=model,
    )
    if tools and resp.success and not resp.tool_calls:
        logger.info(
            "tool_call_native_empty_coax_fallback: model=%s role=%s "
            "(native returned no tool_calls; retrying via coaxing)",
            model, role,
        )
        # §17.610 (audit #29) — the native call above is a real billable request.
        # Record its cost/latency before the coax fallback, else it's lost from
        # llm_call_logs (the caller only records the returned coax response).
        await _record_call(resp)
        return await _tool_call_via_coaxing(
            provider, model, messages, tools,
            temperature=temperature, max_tokens=max_tokens,
            role=role, fallback=fallback,
        )
    if not resp.success and role:
        resp.error = _format_provider_error(resp, role)
    return resp


async def _tool_call_via_coaxing(
    provider,
    model: str,
    messages: list[dict[str, str]],
    tools: list[Tool],
    *,
    temperature: float,
    max_tokens: int,
    role: str | None,
    fallback: str | None,
) -> ModelResponse:
    """Coaxing fallback for providers without native tool support.

    Prepends a system message instructing the model to emit JSON
    matching the FIRST tool's ``input_schema``, then calls chat and
    parses the result. Multi-tool coaxing isn't expressible via a
    single prompt; callers needing that should pin a tool-capable
    provider.

    Empty ``tools`` short-circuits to a plain chat call (no schema
    injection) so the wrapper is a no-op for the empty-tools case.
    """
    if not tools:
        return await _retry_provider_call(
            lambda: provider.chat_completion(
                model, messages, temperature=temperature,
                max_tokens=max_tokens, fallback=fallback,
            ),
            model=model,
        )

    primary = tools[0]
    import json as _json
    schema_text = _json.dumps(primary.input_schema, indent=2)
    coaxing_system = (
        f"You must respond by calling the tool '{primary.name}'.\n"
        f"Tool description: {primary.description}\n\n"
        f"Tool input schema (JSON):\n{schema_text}\n\n"
        f"Respond with ONLY a JSON object matching the schema. "
        f"No prose, no markdown fences."
    )
    augmented = [{"role": "system", "content": coaxing_system}] + list(messages)

    # §17.547 — thinking models routed here (qwen3.5 et al.) spend tokens
    # reasoning before the JSON; floor the budget so a tight caller value
    # (e.g. research extraction's 1024) isn't consumed by reasoning alone.
    effective_max = (
        max(max_tokens, settings.tool_call_coax_min_tokens)
        if _model_lacks_native_tools(model)
        else max_tokens
    )

    resp = await _retry_provider_call(
        lambda: provider.chat_completion(
            model, augmented, temperature=temperature,
            max_tokens=effective_max, fallback=fallback,
        ),
        model=model,
    )
    if not resp.success:
        if role:
            resp.error = _format_provider_error(resp, role)
        return resp

    parsed = parse_json_object(resp.text or "")
    if isinstance(parsed, dict):
        resp.tool_calls = [ToolCall(
            id="coaxed_0",
            name=primary.name,
            arguments=parsed,
        )]
    return resp


async def embed(
    text: str | list[str],
    model: str | None = None,
    *,
    role: str | None = None,
    overrides: dict | None = None,
) -> list[list[float]]:
    """Get embeddings. ``role=`` routes via the provider abstraction.

    Returns a list of vectors regardless of dispatch path.
    """
    _reject_role_model_collision(role, model)
    inputs = text if isinstance(text, list) else [text]
    _begin_trace("embed", messages=[{"role": "input", "content": s} for s in inputs])

    if role:
        resolved_model, provider = _resolve_role(role, overrides)
        # J.3.d — wrap provider.embed in a synthetic ModelResponse so the
        # role-path participates in cost/latency telemetry alongside the
        # legacy direct-Ollama path. Tokens are estimated from input char
        # count (OpenAI's ~4-char-per-token rule of thumb) because
        # LLMProvider.embed returns just the vector list — exact counts
        # would require widening the provider contract. For Ollama the
        # rate is 0 so the estimate doesn't affect cost; for OpenAI it
        # gives an approximate (~10%) cost reading rather than nothing.
        start = time.monotonic()
        embeddings = await provider.embed(resolved_model, inputs)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        from app.providers.base import ModelResponse
        synth = ModelResponse(
            model=resolved_model,
            success=bool(embeddings),
            total_duration_ms=elapsed_ms,
            tokens_prompt=sum(len(s) for s in inputs) // 4,
            tokens_completion=0,
            provider=provider.name,
        )
        await _record_call(synth)
        return embeddings

    model = model or settings.model_embedder_pipeline
    payload: dict[str, Any] = {
        "model": model,
        "input": inputs,
        # §17.545 — see OllamaProvider.embed: truncate to context instead of
        # 400-ing on over-length input (§16.7).
        "truncate": True,
    }
    resp = await _dispatch_with_retry("/api/embed", payload, model, fallback=None)
    await _record_call(resp)
    if not resp.success:
        logger.error("Embedding failed after retries: %s", resp.error)
        return []
    return resp.raw.get("embeddings", [])


async def classify(
    prompt: str,
    model: str | None = None,
    *,
    role: str | None = None,
    overrides: dict | None = None,
) -> ModelResponse:
    """Lightweight classification/routing call (low temperature)."""
    _reject_role_model_collision(role, model)
    if role is None and model is None:
        # Default to the router role (preserves prior behavior of using
        # settings.model_router) — but go through the provider seam so
        # callers benefit from per-role provider routing when configured.
        role = "model_router"
    # §17.606 — model_router defaults to a thinking model (qwen3.5): num_predict
    # is a SHARED reasoning+content budget, so the old max_tokens=256 was
    # consumed by the chain-of-thought and returned empty content with no
    # retry. Route through the shared empty-guard with a generous budget so the
    # reasoning and the short classification answer can coexist (§17.465).
    from app.utils.llm_retry import generate_until_nonempty
    route_kwargs = (
        {"role": role, "overrides": overrides} if role else {"model": model}
    )
    return await generate_until_nonempty(
        generate,
        prompt,
        route_kwargs,
        system="",
        temperature=0.1,
        max_tokens=2048,
        label="classify",
    )


async def list_models() -> list[str]:
    """Return list of available Ollama models."""
    try:
        client = _get_client()
        resp = await client.get(f"{settings.ollama_base_url}/api/tags")
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception as e:
        logger.error("Failed to list models: %s", e)
        return []


async def validate_models(overrides: dict | None = None) -> Optional[list[str]]:
    """
    Check that all Ollama-routed model roles resolve to tags that exist in Ollama.
    Returns:
        list[str]: missing role=tag pairs (empty list => all present)
        None: Ollama unreachable / HTTP error / malformed response
    """
    from app.config import get_model

    OLLAMA_ROLES = [
        "model_general", "model_verifier", "model_coder",
        "model_router", "model_fallback", "model_cloud_alt",
        "model_research_extract",
    ]

    # §17.596 — only validate roles actually routed to Ollama. A role bound to
    # openai/anthropic (via overrides[f"{role}_provider"] or settings.
    # {role}_provider) resolves to a tag like gpt-4o / claude-* that will never
    # appear in Ollama's /api/tags — including it here lands it in `missing` and
    # `_require_valid_models` raises a spurious 422 that blocks core endpoints.
    # Mirrors provider_for_role()'s precedence without importing the provider
    # machinery (this module must stay importable if app.providers fails init).
    def _role_provider(role: str) -> str:
        key = f"{role}_provider"
        if overrides and key in overrides:
            return overrides[key]
        return getattr(settings, key, None) or "ollama"

    needed = {
        role: get_model(role, overrides)
        for role in OLLAMA_ROLES
        if _role_provider(role) == "ollama"
    }

    try:
        resp = await _get_client().get(
            f"{settings.ollama_base_url}/api/tags", timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("validate_models: Ollama unreachable: %s", e)
        return None

    available = set()
    for model in data.get("models", []):
        name = model.get("name", "")
        available.add(name)
        if name.endswith(":latest"):
            available.add(name.removesuffix(":latest"))

    missing = []
    for role, tag in needed.items():
        if tag not in available and f"{tag}:latest" not in available:
            missing.append(f"{role}={tag}")

    return missing
