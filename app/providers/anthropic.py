"""Anthropic Claude provider (§17.345).

Targets ``api.anthropic.com`` by default; ``anthropic_base_url`` allows
pointing at Anthropic-compatible proxies if needed (e.g. an internal
gateway). Auth is ``x-api-key`` (not Bearer) plus the required
``anthropic-version`` header.

Implementation choices
- Raw httpx through the shared client (``app.utils.http_clients.get_anthropic_client``)
  rather than the ``anthropic`` Python SDK. Matches the OpenAIProvider /
  OllamaProvider convention, keeps the dependency surface flat, and makes
  mocking trivial in tests.
- The Messages API shape (system top-level, user/assistant only in
  ``messages``, ``max_tokens`` required) is built inside the provider;
  callers see the unified OpenAI-shape ``[{"role", "content"}]`` and don't
  need to know which backend serves their role.
- Embeddings are NOT supported — Anthropic has no embeddings endpoint.
  ``embed`` raises ``ProviderCapabilityError`` and ``supports_embeddings``
  is ``False`` so dispatcher boot-time validation catches a misconfigured
  embedder role.
- Prompt caching is on by default (``settings.anthropic_prompt_caching``)
  because this is a high-volume routing path. When enabled and ``system``
  is present, ``cache_control: {"type": "ephemeral"}`` is attached to the
  system block (the most likely large stable prefix in a routing call).
  Sub-1024-token prefixes silently won't cache — this is fine, the
  marker is harmless when no cacheable prefix exists.
- Opus 4.7 removes ``temperature`` / ``top_p`` / ``top_k`` (sending any
  of them 400s). The provider strips these fields for ``claude-opus-4-7``
  family models so the default ``temperature=0.7`` from the LLMProvider
  base signature doesn't break the most-capable model.
- Streaming follows Anthropic's SSE shape (``event: ...`` / ``data: ...``
  pairs; ``content_block_delta`` with ``delta.type == "text_delta"``
  carries the text chunks).
- Native tool calls: ``tool_call`` accepts the provider-agnostic ``Tool``
  shape and emits the Anthropic wire format directly — Anthropic's
  ``input_schema`` field already matches our dataclass. The response's
  ``tool_use`` content blocks carry ``input`` as a parsed dict, no JSON
  string decoding required (unlike OpenAI).
"""
from __future__ import annotations

import json as _json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from app.providers.base import (
    LLMProvider,
    ModelResponse,
    ProviderCapabilityError,
    ProviderUnavailableError,
    Tool,
    ToolCall,
)

logger = logging.getLogger("scaffold.providers.anthropic")


# Default max_tokens when caller doesn't specify. Anthropic requires the
# field; the LLMProvider base signature defaults to 4096, which we honor.
_DEFAULT_MAX_TOKENS = 4096

# Model families that REJECT sampling parameters (temperature/top_p/top_k).
# Opus 4.7 removed them entirely — sending any returns 400. Future model
# families that follow the same pattern can be added here. Match is
# prefix-based to cover dated aliases / Bedrock-style suffixes.
_NO_SAMPLING_MODEL_PREFIXES = ("claude-opus-4-7",)


def _apply_anthropic_output_config(payload: dict[str, Any], response_schema: Any) -> None:
    """§17.773 — set Anthropic's ``output_config.format`` from a response schema.

    A JSON Schema ``dict`` becomes a ``json_schema`` format (structured outputs).
    The literal string ``"json"`` has no direct Anthropic equivalent — Anthropic
    has no bare json-object mode — so it is ignored (the caller falls back to the
    llm_parsing repair path). ``None`` / falsy is a no-op. If the caller already
    set ``output_config`` explicitly, that wins and this is a no-op.
    """
    if not response_schema or "output_config" in payload:
        return
    if isinstance(response_schema, dict):
        payload["output_config"] = {
            "format": {"type": "json_schema", "schema": response_schema},
        }
    elif isinstance(response_schema, str):
        logger.info(
            "anthropic_output_config_skipped: 'json' mode has no Anthropic "
            "equivalent; relying on parse fallback",
        )
    else:  # pragma: no cover — defensive; callers pass dict|str|None
        logger.warning(
            "anthropic_output_config_ignored: unexpected response_schema type=%s",
            type(response_schema).__name__,
        )


class AnthropicProvider(LLMProvider):
    """Anthropic Claude backend."""

    name = "anthropic"
    supports_chat = True
    supports_embeddings = False  # Anthropic has no embeddings endpoint
    supports_streaming = True
    supports_native_tools = True
    supports_structured_outputs = True  # §17.773 — output_config.format json_schema

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        """Build per-call ``x-api-key`` + ``anthropic-version`` headers.

        Empty key surfaces as ProviderUnavailableError at call time rather
        than a silent 401 from upstream — same posture as OpenAIProvider.
        """
        from app.config import settings
        key = settings.anthropic_api_key.get_secret_value()
        if not key:
            raise ProviderUnavailableError(
                "anthropic_api_key is empty. Set ANTHROPIC_API_KEY in your "
                "environment (or .env) and re-run, OR change the role's "
                "MODEL_<ROLE>_PROVIDER setting away from 'anthropic'."
            )
        return {
            "x-api-key": key,
            "anthropic-version": settings.anthropic_version,
        }

    @staticmethod
    def _client() -> httpx.AsyncClient:
        from app.utils.http_clients import get_anthropic_client
        return get_anthropic_client()

    @staticmethod
    def _format_http_error(resp: httpx.Response) -> str:
        """Extract the error message from Anthropic's error envelope::

            {"type": "error", "error": {"type": "...", "message": "..."}}

        Falls back to the raw body if the envelope is missing.
        """
        try:
            body = resp.json()
            msg = body.get("error", {}).get("message")
            if msg:
                return f"HTTP {resp.status_code}: {msg}"
        except Exception:
            pass
        return f"HTTP {resp.status_code}: {resp.text[:200]}"

    @staticmethod
    def _strips_sampling(model: str) -> bool:
        """Whether the target model rejects temperature/top_p/top_k."""
        return any(model.startswith(p) for p in _NO_SAMPLING_MODEL_PREFIXES)

    @staticmethod
    def _split_system(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, Any]]]:
        """Anthropic puts the system prompt top-level, NOT inside ``messages``.

        Walk the input (OpenAI shape) and:
        - concatenate any ``role=system`` entries into a single system string
          (joined with double-newline if there are multiple — uncommon but
          tolerated by the OpenAI shape)
        - keep ``user`` / ``assistant`` entries in the messages list

        Returns ``(system_or_None, anthropic_messages)``.
        """
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                if content:
                    system_parts.append(content if isinstance(content, str) else str(content))
                continue
            if role in ("user", "assistant"):
                out.append({"role": role, "content": content})
            # Silently drop unknown roles — Anthropic would 400 on them.
        system = "\n\n".join(system_parts) if system_parts else None
        return system, out

    def _build_payload(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        opts: dict[str, Any],
    ) -> dict[str, Any]:
        """Assemble the Anthropic request body from the unified call shape.

        Centralizes: system extraction, sampling-param stripping for
        Opus 4.7, prompt-caching application, and opts pass-through. Used
        by both ``chat_completion`` and ``tool_call`` so behavior stays
        consistent across the two entry points.
        """
        from app.config import settings

        system, anthropic_messages = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages,
        }
        if system is not None:
            # When prompt caching is enabled, wrap system in the block form
            # so we can attach cache_control. This is the prefix most likely
            # to be stable across requests in a routing workload.
            if settings.anthropic_prompt_caching:
                payload["system"] = [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                payload["system"] = system
        if not self._strips_sampling(model):
            payload["temperature"] = temperature
        # §17.773 — grammar-constrained decoding: translate the provider-agnostic
        # ``response_schema`` into Anthropic's structured-outputs
        # ``output_config.format`` (the ``output_format`` parameter is deprecated).
        # NOTE: Anthropic enforces the schema — every object must carry
        # ``additionalProperties: false`` with all keys required, or the request
        # 400s. Callers targeting the Anthropic backend must pass a compliant
        # schema; the default-off valve keeps this off the hot path until then.
        _apply_anthropic_output_config(payload, opts.get("response_schema"))
        # Pass through caller extras (thinking, output_config, tools,
        # tool_choice, top_p, ...). Drop ``fallback`` (Ollama-specific),
        # ``response_schema`` (handled above), and anything we already placed.
        reserved = {
            "fallback", "stream", "model", "messages", "max_tokens",
            "system", "temperature", "response_schema",
        }
        for k, v in opts.items():
            if k in reserved:
                continue
            payload[k] = v
        return payload

    # ------------------------------------------------------------------
    # chat_completion (POST /messages)
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: int = 600,
        **opts: Any,
    ) -> ModelResponse:
        from app.config import settings
        payload = self._build_payload(
            model, messages,
            temperature=temperature, max_tokens=max_tokens, opts=opts,
        )
        payload["stream"] = False
        effective_timeout = timeout if timeout != 600 else settings.anthropic_timeout
        return await self._request(
            "/messages", payload, model, effective_timeout,
            text_extractor=self._extract_chat_text,
        )

    # ------------------------------------------------------------------
    # embed — not supported
    # ------------------------------------------------------------------

    async def embed(
        self,
        model: str,
        texts: list[str],
        *,
        timeout: int = 120,
    ) -> list[list[float]]:
        """Anthropic does not expose an embeddings endpoint.

        Raises ``ProviderCapabilityError`` so dispatcher boot-time
        validation surfaces the misconfiguration clearly. Operators
        needing embeddings should bind the embedder role to ``ollama``
        or ``openai`` (or any other provider that supports them).
        """
        raise ProviderCapabilityError(
            "anthropic provider does not support embeddings; bind "
            "model_embedder_pipeline_provider to 'ollama' or 'openai'"
        )

    # ------------------------------------------------------------------
    # stream_chat — SSE event stream
    # ------------------------------------------------------------------

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: int = 600,
        **opts: Any,
    ) -> AsyncIterator[str]:
        """Stream text deltas from /messages (stream=true).

        Parses Anthropic's SSE shape — interleaved ``event: <name>`` /
        ``data: {...}`` line pairs — and yields the ``delta.text`` of each
        ``content_block_delta`` whose delta type is ``text_delta``. Thinking
        blocks (``thinking_delta``) are intentionally NOT yielded; callers
        wanting reasoning text should consume the unified ``chat_completion``
        response and inspect ``raw.content``.

        Same retry posture as OpenAIProvider's streaming: no mid-stream
        retry (caller can re-issue if needed).
        """
        from app.config import settings

        payload = self._build_payload(
            model, messages,
            temperature=temperature, max_tokens=max_tokens, opts=opts,
        )
        payload["stream"] = True

        headers = self._auth_headers()  # may raise ProviderUnavailableError
        effective_timeout = timeout if timeout != 600 else settings.anthropic_timeout

        async with self._client().stream(
            "POST", "/messages",
            json=payload, headers=headers, timeout=effective_timeout,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                snippet = body[:200].decode("utf-8", errors="replace")
                raise ProviderUnavailableError(
                    f"anthropic HTTP {resp.status_code}: {snippet}"
                )
            async for line in resp.aiter_lines():
                # Anthropic SSE: ``event: <name>\n`` followed by
                # ``data: <json>\n\n``. We only need the data lines —
                # event names are advisory.
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if not data_str:
                    continue
                try:
                    chunk = _json.loads(data_str)
                except ValueError:
                    continue
                if chunk.get("type") == "error":
                    # §17.610 (audit #38) — Anthropic can deliver a mid-stream
                    # error frame (e.g. overloaded_error) on a 200 stream. Without
                    # this branch the loop `continue`s past it and the generator
                    # ends silently, so a consumer that already received partial
                    # content accepts a truncated result as complete. Propagate it
                    # like the non-stream error path.
                    err = chunk.get("error") or {}
                    msg = err.get("message") or err.get("type") or "unknown error"
                    raise ProviderUnavailableError(f"anthropic stream error: {msg}")
                if chunk.get("type") != "content_block_delta":
                    continue
                delta = chunk.get("delta") or {}
                if delta.get("type") != "text_delta":
                    continue
                text = delta.get("text")
                if text:
                    yield text

    # ------------------------------------------------------------------
    # tool_call (POST /messages with tools=[...])
    # ------------------------------------------------------------------

    @staticmethod
    def _tools_to_anthropic(tools: list[Tool]) -> list[dict[str, Any]]:
        """Translate provider-agnostic Tool list to Anthropic's wire shape::

            [{"name": ..., "description": ..., "input_schema": <JSON Schema>}]

        Anthropic uses ``input_schema`` (same name as our dataclass field)
        and does NOT wrap each tool in a ``{"type": "function", ...}``
        envelope the way OpenAI does — flat list of tool objects.
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

    @staticmethod
    def _tool_choice_to_anthropic(choice: str) -> dict[str, Any]:
        """OpenAI-style strings translate to Anthropic's typed objects::

            "auto"     → {"type": "auto"}
            "any"      → {"type": "any"}
            "required" → {"type": "any"}   (alias — Anthropic uses "any")
            "none"     → {"type": "none"}  (provider-level; agent-design
                                            note: agents commonly omit
                                            tools entirely instead)
            "<name>"   → {"type": "tool", "name": "<name>"}
        """
        if choice == "auto":
            return {"type": "auto"}
        if choice in ("any", "required"):
            return {"type": "any"}
        if choice == "none":
            return {"type": "none"}
        return {"type": "tool", "name": choice}

    async def tool_call(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[Tool],
        *,
        temperature: float = 0.7,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: int = 600,
        tool_choice: str = "auto",
        **opts: Any,
    ) -> ModelResponse:
        from app.config import settings

        # Build base payload, then layer tool fields on top.
        payload = self._build_payload(
            model, messages,
            temperature=temperature, max_tokens=max_tokens, opts=opts,
        )
        payload["stream"] = False
        payload["tools"] = self._tools_to_anthropic(tools)
        payload["tool_choice"] = self._tool_choice_to_anthropic(tool_choice)
        effective_timeout = timeout if timeout != 600 else settings.anthropic_timeout

        return await self._request(
            "/messages", payload, model, effective_timeout,
            text_extractor=self._extract_chat_text,
            tool_calls_extractor=self._extract_tool_calls,
        )

    @staticmethod
    def _extract_tool_calls(data: dict[str, Any]) -> list[ToolCall]:
        """Pull ``tool_use`` content blocks out of the Anthropic response.

        Unlike OpenAI (where ``function.arguments`` is a JSON-encoded
        string), Anthropic emits ``input`` as a parsed dict already — no
        json.loads needed. A malformed input (non-dict) is normalized to
        ``{}`` so the response stays parseable.
        """
        content = data.get("content") or []
        out: list[ToolCall] = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            inp = block.get("input")
            out.append(ToolCall(
                id=block.get("id") or "",
                name=block.get("name") or "",
                arguments=inp if isinstance(inp, dict) else {},
            ))
        return out

    # ------------------------------------------------------------------
    # list_models (GET /models)
    # ------------------------------------------------------------------

    async def list_models(self) -> list[str]:
        try:
            headers = self._auth_headers()
        except ProviderUnavailableError as exc:
            logger.warning("anthropic_list_models_unavailable: %s", exc)
            return []
        try:
            resp = await self._client().get("/models", headers=headers)
        except Exception as exc:
            logger.error("anthropic_list_models_exception: %s", exc)
            return []
        if resp.status_code != 200:
            logger.error("anthropic_list_models_http_error: %s", self._format_http_error(resp))
            return []
        data = resp.json()
        return [item.get("id", "") for item in data.get("data", []) if item.get("id")]

    # ------------------------------------------------------------------
    # Internal: request + parse helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_chat_text(data: dict[str, Any]) -> str:
        """Concatenate text from all ``text``-type content blocks.

        Anthropic's response is ``content: [{type: text|tool_use|thinking, ...}]``
        — text blocks carry the user-visible reply. We join multiple text
        blocks with empty string (the model rarely splits mid-paragraph,
        but defensive concatenation preserves whatever the model emitted).
        Thinking and tool_use blocks are skipped — tool_use is exposed via
        ModelResponse.tool_calls; thinking is reasoning, not response text.
        """
        content = data.get("content") or []
        parts: list[str] = []
        for block in content:
            if block.get("type") == "text":
                t = block.get("text")
                if t:
                    parts.append(t)
        return "".join(parts)

    async def _request(
        self,
        endpoint: str,
        payload: dict[str, Any],
        model: str,
        timeout: int,
        *,
        text_extractor,
        tool_calls_extractor=None,
    ) -> ModelResponse:
        try:
            headers = self._auth_headers()
        except ProviderUnavailableError as exc:
            return ModelResponse(
                model=model, success=False, error=str(exc),
                provider=self.name,
            )

        start = time.monotonic()
        try:
            resp = await self._client().post(
                endpoint, json=payload, headers=headers, timeout=timeout,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code != 200:
                return ModelResponse(
                    model=model, success=False,
                    error=self._format_http_error(resp),
                    total_duration_ms=elapsed_ms,
                    provider=self.name,
                )
            data = resp.json()
            text = text_extractor(data)
            # Anthropic usage shape:
            #   {input_tokens, output_tokens,
            #    cache_creation_input_tokens, cache_read_input_tokens}
            # We sum input_tokens + cache_* to get total prompt tokens
            # seen by the model (cache reads still count toward "tokens
            # the model processed"); cost-tracking downstream pulls the
            # raw shape from ModelResponse.raw if it wants the breakdown.
            usage = data.get("usage") or {}
            tokens_prompt = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
            ) or None
            tokens_completion = usage.get("output_tokens")
            tps = None
            if tokens_completion and elapsed_ms > 0:
                tps = round(tokens_completion / (elapsed_ms / 1000), 2)
            tool_calls = tool_calls_extractor(data) if tool_calls_extractor else []
            return ModelResponse(
                text=(text or "").strip(),
                model=data.get("model", model),
                success=True,
                total_duration_ms=elapsed_ms,
                tokens_prompt=tokens_prompt,
                tokens_completion=tokens_completion,
                tokens_per_sec=tps,
                provider=self.name,
                raw=data,
                tool_calls=tool_calls,
            )
        except httpx.TimeoutException:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ModelResponse(
                model=model, success=False,
                error=f"Timeout after {timeout}s",
                total_duration_ms=elapsed_ms,
                provider=self.name,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ModelResponse(
                model=model, success=False, error=str(exc),
                total_duration_ms=elapsed_ms,
                provider=self.name,
            )


# Register the singleton at import time. ``app/providers/__init__.py``
# triggers this via its ``_autoload`` helper.
from app.providers import register  # noqa: E402

register("anthropic", AnthropicProvider())
