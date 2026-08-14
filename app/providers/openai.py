"""OpenAI-compatible provider.

Targets ``api.openai.com`` by default but works against any OpenAI-compatible
endpoint (vLLM, LocalAI, Ollama in OpenAI mode, …) via the ``OPENAI_BASE_URL``
setting. That's the value of using the OpenAI shape: one provider implementation
covers a wide ecosystem of servers.

Implementation choices
- Raw httpx through the shared client (``app.utils.http_clients.get_openai_client``)
  rather than the ``openai`` Python SDK. Keeps the dependency surface flat,
  matches the OllamaProvider pattern, and makes mocking trivial.
- Auth header built per-call so a key rotation doesn't require rebuilding
  the client.
- ``dimensions=embedding_dim`` is sent on every embed call so server output
  matches the orchestrator's locked 512-dim shape. Models that ignore this
  parameter (e.g. ``text-embedding-ada-002``) will return their native
  dim — the orchestrator's downstream truncate_and_normalize handles that.
- Streaming is implemented as of Sprint I.1 (``stream_chat`` consumes
  the OpenAI SSE shape — ``data: {...}\\n\\n`` chunks plus ``[DONE]``
  terminator — and yields ``str`` deltas, matching OllamaProvider's
  unified streaming contract).
- Native tool calls (Sprint I.2): ``tool_call`` accepts the
  provider-agnostic ``Tool`` shape and translates to OpenAI's
  ``{type: "function", function: {...}}`` wire format. The response's
  ``function.arguments`` is a JSON-encoded string — we decode it back
  to a dict so callers receive structured arguments, not a string they
  have to re-parse.
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
    ProviderUnavailableError,
    Tool,
    ToolCall,
)

logger = logging.getLogger("scaffold.providers.openai")


def _apply_openai_response_format(payload: dict[str, Any], response_schema: Any) -> None:
    """§17.773 — set OpenAI's ``response_format`` from a response schema.

    A JSON Schema ``dict`` becomes a ``json_schema`` response format (constrain to
    that shape, ``strict=false``). The literal string ``"json"`` maps to the older
    ``json_object`` mode (any valid JSON). ``None`` / falsy is a no-op.
    """
    if not response_schema:
        return
    if isinstance(response_schema, str):
        payload["response_format"] = {"type": "json_object"}
    elif isinstance(response_schema, dict):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "scaffold_response",
                "schema": response_schema,
                "strict": False,
            },
        }
    else:  # pragma: no cover — defensive; callers pass dict|str|None
        logger.warning(
            "openai_response_format_ignored: unexpected response_schema type=%s",
            type(response_schema).__name__,
        )


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible backend."""

    name = "openai"
    supports_chat = True
    supports_embeddings = True
    supports_streaming = True
    supports_structured_outputs = True  # §17.773 — response_format json_schema
    supports_native_tools = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        """Build per-call Authorization header. Empty key surfaces as
        ProviderUnavailableError at call time rather than a silent 401."""
        from app.config import settings
        key = settings.openai_api_key.get_secret_value()
        if not key:
            raise ProviderUnavailableError(
                "openai_api_key is empty. Set OPENAI_API_KEY in your "
                "environment (or .env) and re-run, OR change the role's "
                "MODEL_<ROLE>_PROVIDER setting away from 'openai'."
            )
        return {"Authorization": f"Bearer {key}"}

    @staticmethod
    def _client() -> httpx.AsyncClient:
        from app.utils.http_clients import get_openai_client
        return get_openai_client()

    @staticmethod
    def _format_http_error(resp: httpx.Response) -> str:
        """Extract the error message from an OpenAI error envelope, falling
        back to the raw body. Provider returns this in ModelResponse.error
        so users see the upstream reason, not a generic HTTP code."""
        try:
            body = resp.json()
            msg = body.get("error", {}).get("message")
            if msg:
                return f"HTTP {resp.status_code}: {msg}"
        except Exception:
            pass
        return f"HTTP {resp.status_code}: {resp.text[:200]}"

    # ------------------------------------------------------------------
    # chat_completion (POST /chat/completions)
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,
        **opts: Any,
    ) -> ModelResponse:
        from app.config import settings
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # §17.773 — grammar-constrained decoding: translate the provider-agnostic
        # ``response_schema`` into OpenAI's ``response_format`` json_schema. strict
        # is False because the schemas threaded here aren't guaranteed to satisfy
        # OpenAI's strict-mode requirements (every object needs
        # additionalProperties:false and all-required); strict=false still
        # constrains to schema-shaped JSON without 400-ing on a lenient schema.
        _apply_openai_response_format(payload, opts.get("response_schema"))
        # Pass through any caller extras (response_format, top_p, …) that the
        # OpenAI API understands. ``fallback`` is Ollama-specific and is
        # ignored here on purpose; ``response_schema`` is handled above.
        for k, v in opts.items():
            if k in {"fallback", "response_schema"}:
                continue
            payload[k] = v

        effective_timeout = timeout if timeout != 600 else settings.openai_timeout
        return await self._request(
            "/chat/completions", payload, model, effective_timeout,
            text_extractor=self._extract_chat_text,
        )

    # ------------------------------------------------------------------
    # embed (POST /embeddings)
    # ------------------------------------------------------------------

    async def embed(
        self,
        model: str,
        texts: list[str],
        *,
        timeout: int = 120,
    ) -> list[list[float]]:
        from app.config import settings
        payload: dict[str, Any] = {
            "model": model,
            "input": texts,
            "dimensions": settings.embedding_dim,
            "encoding_format": "float",
        }
        try:
            headers = self._auth_headers()
        except ProviderUnavailableError as exc:
            logger.error("openai_embed_unavailable: %s", exc)
            return []
        try:
            resp = await self._client().post(
                "/embeddings", json=payload, headers=headers, timeout=timeout,
            )
        except httpx.TimeoutException:
            logger.error("openai_embed_timeout after %ds", timeout)
            return []
        except Exception as exc:
            logger.error("openai_embed_exception: %s", exc)
            return []

        if resp.status_code != 200:
            logger.error("openai_embed_http_error: %s", self._format_http_error(resp))
            return []
        data = resp.json()
        items = data.get("data", [])
        # Sort by index defensively — the API returns in input order but the
        # spec guarantees only that the index field marks the original slot.
        items.sort(key=lambda x: x.get("index", 0))
        return [item.get("embedding", []) for item in items]

    # ------------------------------------------------------------------
    # stream_chat — Sprint I.1
    # ------------------------------------------------------------------

    async def stream_chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,
        **opts: Any,
    ) -> AsyncIterator[str]:
        """Stream content deltas from /chat/completions (stream=True).

        Parses OpenAI's Server-Sent-Events (``data: {...}\\n\\n`` lines plus
        a ``data: [DONE]\\n\\n`` terminator), yielding the
        ``choices[0].delta.content`` of each chunk as a ``str`` — the
        unified provider streaming shape (matches OllamaProvider).

        ``ProviderUnavailableError`` is raised on missing API key (via
        ``_auth_headers``) or non-200 upstream — surfacing the OpenAI error
        message verbatim so users can act. Same retry-policy stance as
        OllamaProvider: streaming does NOT retry on mid-stream failure
        (caller can re-issue the call if they want to).
        """
        from app.config import settings

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        for k, v in opts.items():
            if k == "fallback":  # Ollama-specific; not part of OpenAI's API
                continue
            payload[k] = v

        headers = self._auth_headers()  # may raise ProviderUnavailableError
        effective_timeout = timeout if timeout != 600 else settings.openai_timeout

        async with self._client().stream(
            "POST", "/chat/completions",
            json=payload, headers=headers, timeout=effective_timeout,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                snippet = body[:200].decode("utf-8", errors="replace")
                raise ProviderUnavailableError(
                    f"openai HTTP {resp.status_code}: {snippet}"
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    # OpenAI also emits ``event: ...`` and blank lines. Skip
                    # anything that isn't a data frame.
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    return
                try:
                    chunk = _json.loads(data_str)
                except ValueError:
                    # Partial frame — should never happen with aiter_lines
                    # but defensive: skip and keep streaming.
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content

    # ------------------------------------------------------------------
    # tool_call — Sprint I.2 (POST /chat/completions with tools=[...])
    # ------------------------------------------------------------------

    @staticmethod
    def _tools_to_openai(tools: list[Tool]) -> list[dict[str, Any]]:
        """Translate provider-agnostic Tool list to OpenAI's wire shape::

            [{"type": "function",
              "function": {"name": ..., "description": ...,
                           "parameters": <JSON Schema>}}]
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    @staticmethod
    def _tool_choice_to_openai(choice: str) -> Any:
        """OpenAI accepts ``"auto"``/``"none"``/``"required"`` as bare
        strings, but a specific tool name has to be wrapped::

            {"type": "function", "function": {"name": "<name>"}}
        """
        if choice in {"auto", "none", "required"}:
            return choice
        return {"type": "function", "function": {"name": choice}}

    async def tool_call(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[Tool],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,
        tool_choice: str = "auto",
        **opts: Any,
    ) -> ModelResponse:
        from app.config import settings

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": self._tools_to_openai(tools),
            "tool_choice": self._tool_choice_to_openai(tool_choice),
            "stream": False,
        }
        for k, v in opts.items():
            if k == "fallback":
                continue
            payload[k] = v
        effective_timeout = timeout if timeout != 600 else settings.openai_timeout

        return await self._request(
            "/chat/completions", payload, model, effective_timeout,
            text_extractor=self._extract_chat_text,
            tool_calls_extractor=self._extract_tool_calls,
        )

    @staticmethod
    def _extract_tool_calls(data: dict[str, Any]) -> list[ToolCall]:
        """Pull tool_calls out of the OpenAI response envelope.

        OpenAI emits ``function.arguments`` as a JSON-encoded string —
        we decode it back to a dict so callers don't have to. A malformed
        argument blob is treated as ``{}`` rather than raising; the model
        misbehaved, but the call itself succeeded enough that we want
        the rest of the response visible.
        """
        choices = data.get("choices") or []
        if not choices:
            return []
        message = choices[0].get("message") or {}
        raw_calls = message.get("tool_calls") or []
        out: list[ToolCall] = []
        for call in raw_calls:
            fn = call.get("function") or {}
            args_str = fn.get("arguments") or "{}"
            try:
                args = _json.loads(args_str) if isinstance(args_str, str) else args_str
            except _json.JSONDecodeError:
                args = {}
            out.append(ToolCall(
                id=call.get("id") or "",
                name=fn.get("name") or "",
                arguments=args if isinstance(args, dict) else {},
            ))
        return out

    # ------------------------------------------------------------------
    # list_models (GET /models)
    # ------------------------------------------------------------------

    async def list_models(self) -> list[str]:
        try:
            headers = self._auth_headers()
        except ProviderUnavailableError as exc:
            logger.warning("openai_list_models_unavailable: %s", exc)
            return []
        try:
            resp = await self._client().get("/models", headers=headers)
        except Exception as exc:
            logger.error("openai_list_models_exception: %s", exc)
            return []
        if resp.status_code != 200:
            logger.error("openai_list_models_http_error: %s", self._format_http_error(resp))
            return []
        data = resp.json()
        return [item.get("id", "") for item in data.get("data", []) if item.get("id")]

    # ------------------------------------------------------------------
    # Internal: request + parse helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_chat_text(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("message") or {}).get("content", "") or ""

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
            usage = data.get("usage") or {}
            tokens_prompt = usage.get("prompt_tokens")
            tokens_completion = usage.get("completion_tokens")
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

register("openai", OpenAIProvider())
