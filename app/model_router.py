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
import time
from typing import Any, Optional

import httpx

from app.config import settings
from app.providers.base import ModelResponse, Tool, ToolCall  # noqa: F401 — public re-export
from app.utils.llm_parsing import parse_json_object

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
    return model in CLOUD_MODELS or model.endswith("-cloud")

def _timeout_for(model: str) -> int:
    return settings.cloud_timeout if _is_cloud(model) else settings.local_timeout


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
_RETRYABLE_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

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
    if err.startswith("HTTP "):
        try:
            code = int(err.split(None, 2)[1].rstrip(":"))
        except (IndexError, ValueError):
            return "retry"
        return "retry" if code in _RETRYABLE_HTTP_CODES else "fail_fast"
    # Generic exception path (httpx connect errors, RST, DNS, etc.) —
    # almost always transient, so retry.
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
    return resp


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
) -> ModelResponse:
    """Generate text. ``role=`` routes via the provider abstraction.

    Pass ``role="model_general"`` (etc.) to dispatch through whichever
    provider is bound to that role in settings/overrides. Pass ``model=``
    for the legacy direct-Ollama path. The two are mutually exclusive.
    """
    _reject_role_model_collision(role, model)
    if role:
        resolved_model, provider = _resolve_role(role, overrides)
        resp = await provider.generate(
            resolved_model, prompt,
            system=system, temperature=temperature, max_tokens=max_tokens,
            fallback=fallback,
        )
        if not resp.success:
            resp.error = _format_provider_error(resp, role)
        return await _record_call(resp)

    model = model or settings.model_general
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if system:
        payload["system"] = system
    resp = await _dispatch_with_retry("/api/generate", payload, model, fallback)
    return await _record_call(resp)


async def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    *,
    role: str | None = None,
    overrides: dict | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    fallback: str | None = None,
) -> ModelResponse:
    """Chat completion. ``role=`` routes via the provider abstraction."""
    _reject_role_model_collision(role, model)
    if role:
        resolved_model, provider = _resolve_role(role, overrides)
        resp = await provider.chat_completion(
            resolved_model, messages,
            temperature=temperature, max_tokens=max_tokens,
            fallback=fallback,
        )
        if not resp.success:
            resp.error = _format_provider_error(resp, role)
        return await _record_call(resp)

    model = model or settings.model_general
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
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
    """
    _reject_role_model_collision(role, model)

    if role:
        resolved_model, provider = _resolve_role(role, overrides)
        if getattr(provider, "supports_native_tools", False):
            resp = await provider.tool_call(
                resolved_model, messages, tools,
                temperature=temperature, max_tokens=max_tokens,
                tool_choice=tool_choice,
            )
            if not resp.success:
                resp.error = _format_provider_error(resp, role)
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
    if getattr(provider, "supports_native_tools", False):
        resp = await provider.tool_call(
            model, messages, tools,
            temperature=temperature, max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
        return await _record_call(resp)
    coaxed = await _tool_call_via_coaxing(
        provider, model, messages, tools,
        temperature=temperature, max_tokens=max_tokens,
        role=None, fallback=fallback,
    )
    return await _record_call(coaxed)


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
        return await provider.chat_completion(
            model, messages, temperature=temperature,
            max_tokens=max_tokens, fallback=fallback,
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

    resp = await provider.chat_completion(
        model, augmented, temperature=temperature,
        max_tokens=max_tokens, fallback=fallback,
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
    return await generate(
        prompt, model,
        role=role, overrides=overrides,
        temperature=0.1, max_tokens=256, fallback=None,
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
    ]
    needed = {role: get_model(role, overrides) for role in OLLAMA_ROLES}

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
