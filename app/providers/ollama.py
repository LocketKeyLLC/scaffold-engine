"""Ollama provider — thin adapter over ``app.model_router``.

The retry/fallback/timeout logic lives in ``app.model_router._dispatch_with_retry``
and the single-call HTTP path lives in ``app.model_router._call_ollama``. This
class delegates to those functions instead of re-implementing them so that:

  1. Tests that ``patch.object(model_router, "_call_ollama", ...)`` continue
     to intercept every dispatch path — including calls reached through
     ``provider_for_role``.
  2. Behavior stays bit-identical for existing callers; the provider
     abstraction only adds a routing seam, not a second implementation.

The exception is ``stream_chat`` (Sprint I.1): streaming has different
retry semantics — a mid-stream failure can't cleanly fall back without
restarting from the first token — so it goes direct to the shared HTTP
client and handles its own timeout/error path. ``chat_completion`` keeps
the retry+fallback path for the non-streaming case.

Adding non-Ollama providers later (OpenAI, Anthropic, …) means writing a
self-contained module — those backends do not share Ollama's HTTP shape so
they get their own dispatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from app.providers.base import (
    LLMProvider,
    ModelResponse,
    ProviderUnavailableError,
    Tool,
    ToolCall,
)

logger = logging.getLogger("scaffold.providers.ollama")


def _apply_ollama_format(payload: dict[str, Any], response_schema: Any) -> None:
    """§17.773 — set Ollama's ``format`` from a provider-agnostic response schema.

    ``response_schema`` is a JSON Schema ``dict`` (constrain to that shape) or the
    literal string ``"json"`` (constrain to any valid JSON). ``None`` / falsy is a
    no-op so unconstrained callers are unchanged. The value is passed to Ollama
    verbatim — Ollama's ``format`` field accepts exactly these two shapes.
    """
    if not response_schema:
        return
    if isinstance(response_schema, (dict, str)):
        payload["format"] = response_schema
    else:  # pragma: no cover — defensive; callers pass dict|str|None
        logger.warning(
            "ollama_format_ignored: unexpected response_schema type=%s",
            type(response_schema).__name__,
        )


class OllamaProvider(LLMProvider):
    """Local-or-bridge Ollama backend. Delegates to ``app.model_router``."""

    name = "ollama"
    supports_chat = True
    supports_embeddings = True
    supports_streaming = True
    # Sprint I.2: Ollama 0.3+ accepts the OpenAI-shape tools field on
    # /api/chat. Tool support is model-dependent (qwen2.5, llama3.1+, …);
    # this flag advertises the provider's capability — mismatched models
    # will simply ignore the tools field and respond with text.
    supports_native_tools = True
    # §17.773 — False: /api/generate accepts a `format` schema, but the
    # CLOUD-proxied models this engine runs (kimi/deepseek/glm/qwen3.5) silently
    # ignore it (live smoke — constrained output == baseline, both fenced). Local
    # Ollama honors GBNF grammars, so a local-model deployment opts back in via
    # the `structured_outputs_ollama_enabled` valve rather than flipping this flag.
    supports_structured_outputs = False

    async def chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,  # noqa: ARG002 — model_router resolves cloud vs local
        fallback: str | None = None,
        **opts: Any,
    ) -> ModelResponse:
        from app import model_router
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        # §17.876 — optional thinking toggle, same semantics as ``generate``
        # below (§17.683): num_predict is a SHARED thinking+content budget and
        # only ``message.content`` is read, so a long chain-of-thought can
        # consume the whole budget and return empty content. ``think=False``
        # sends all tokens to content. Absent/None → unchanged model default.
        think = opts.get("think")
        if think is not None:
            payload["think"] = think
        # §17.773 — grammar-constrained decoding. A JSON Schema in ``format``
        # makes Ollama constrain generation to schema-valid JSON (llama.cpp GBNF
        # for local models; cloud-proxied models vary — hence the default-off
        # valve upstream). Absent → unconstrained, as before.
        _apply_ollama_format(payload, opts.get("response_schema"))
        return await model_router._dispatch_with_retry(
            "/api/chat", payload, model, fallback,
        )

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,  # noqa: ARG002 — model_router resolves cloud vs local
        fallback: str | None = None,
        **opts: Any,
    ) -> ModelResponse:
        from app import model_router
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if system:
            payload["system"] = system
        # §17.683 — optional thinking toggle. On a reasoning model num_predict is
        # a SHARED thinking+content budget and model_router reads ONLY `response`
        # (the `thinking` field is discarded), so a long chain-of-thought starves
        # (or empties) the answer. `think=False` sends all tokens to `response`.
        # Absent/None → unchanged model default.
        think = opts.get("think")
        if think is not None:
            payload["think"] = think
        # §17.773 — see chat_completion. Ollama accepts a JSON Schema in ``format``
        # on /api/generate as well.
        _apply_ollama_format(payload, opts.get("response_schema"))
        return await model_router._dispatch_with_retry(
            "/api/generate", payload, model, fallback,
        )

    async def embed(
        self,
        model: str,
        texts: list[str],
        *,
        timeout: int | None = None,
    ) -> list[list[float]]:
        """Embed `texts` via Ollama's /api/embed.

        Audit Finding B (root cause): a wedged Ollama embedder runner
        previously left the orchestrator waiting on the dispatcher's
        legacy ``settings.local_timeout`` (30 min default) before
        surfacing the failure. SSE consumers saw 30 minutes of silence,
        and a /research that should have completed in 5-10 minutes
        instead returned a curl timeout.

        Now bounded by ``asyncio.wait_for`` with a per-call cap derived
        from input count: ``max(120, 30 * n_texts)`` seconds, capped at
        600s. For 12 chunks that's 360s (6 min) — leaving substantial
        headroom for legitimate CPU-only embed runs while surfacing
        Ollama-side wedges roughly 4x faster.

        On timeout returns an empty list and logs ``embed_timeout``;
        callers (rag_pipeline._embed_contents_batch, ingest_entries)
        already handle the empty-list case by treating individual
        entries as "embed failed" and skipping them in upsert.
        """
        from app import model_router
        if timeout is None:
            timeout = max(120, 30 * len(texts))
            timeout = min(timeout, 600)
        payload: dict[str, Any] = {
            "model": model,
            "input": texts,
            # §17.545 — explicitly truncate to the model's context. Ollama's
            # /api/embed default is normally truncate=true, but a context-length
            # 400 ("input length exceeds the context length") was seen in prod
            # (§16.7); truncate=false reproduces that exact error. Setting it
            # true makes over-length inputs head-truncate to the embedder's
            # context instead of failing the embed (and dropping the entry).
            "truncate": True,
        }
        try:
            resp = await asyncio.wait_for(
                model_router._dispatch_with_retry(
                    "/api/embed", payload, model, fallback=None,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                "embed_timeout: model=%s n_texts=%d timeout_s=%d "
                "(suggests a wedged Ollama runner; restart `ollama` daemon)",
                model, len(texts), timeout,
            )
            return []
        if not resp.success:
            logger.error("Embedding failed after retries: %s", resp.error)
            return []
        return resp.raw.get("embeddings", [])

    async def list_models(self) -> list[str]:
        from app import model_router
        return await model_router.list_models()

    # ------------------------------------------------------------------
    # tool_call — Sprint I.2 (POST /api/chat with tools=[...])
    # ------------------------------------------------------------------

    @staticmethod
    def _tools_to_ollama(tools: list[Tool]) -> list[dict[str, Any]]:
        """Translate provider-agnostic Tool list to Ollama's wire shape.

        Ollama copies OpenAI's structure verbatim::

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
    def _extract_ollama_tool_calls(
        message: dict[str, Any],
    ) -> list[ToolCall]:
        """Pull tool_calls out of an Ollama response message.

        Ollama differs from OpenAI in two notable ways:

        1. ``arguments`` is a dict directly — NOT a JSON-encoded string.
        2. There is no ``id`` field per call; we synthesize ``tool_<index>``
           so callers that need an id (multi-turn tool/result threads)
           still have one to thread.
        """
        raw_calls = message.get("tool_calls") or []
        out: list[ToolCall] = []
        for i, call in enumerate(raw_calls):
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                # Some Ollama builds (or compat shims) emit a string —
                # decode for parity with OpenAI's parsed shape.
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            out.append(ToolCall(
                id=call.get("id") or f"tool_{i}",
                name=fn.get("name") or "",
                arguments=args,
            ))
        return out

    async def tool_call(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[Tool],
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 600,
        tool_choice: str = "auto",  # noqa: ARG002 — Ollama always negotiates choice with the model
        **opts: Any,
    ) -> ModelResponse:
        """Native tool calling via Ollama 0.3+.

        Tool support is model-dependent — qwen2.5, llama3.1+, mistral-nemo,
        and a few others speak the protocol; older or smaller models
        (qwen3:4b, qwen2.5:3b, …) often respond with text only and ignore
        the tools field. The capability flag advertises provider support;
        the caller picks an appropriate model for the role.

        Note: Ollama doesn't expose a ``tool_choice`` parameter, so the
        argument is accepted (for cross-provider parity) but not threaded
        to the wire. ``"none"`` callers should pin a non-tool model
        instead, or use ``chat_completion``.

        Goes direct to the shared HTTP client (no retry/fallback) so a
        partial response or model-side ignore surfaces immediately rather
        than re-running the whole tool-orchestration plan.
        """
        from app import model_router
        from app.config import settings

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "tools": self._tools_to_ollama(tools),
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        for k, v in opts.items():
            if k in {"fallback"}:  # not part of the Ollama API
                continue
            payload[k] = v

        url = f"{settings.ollama_base_url}/api/chat"
        effective_timeout = (
            timeout if timeout != 600 else model_router._timeout_for(model)
        )
        client = model_router._get_client()

        import time as _time
        start = _time.monotonic()
        try:
            resp = await client.post(url, json=payload, timeout=effective_timeout)
            elapsed_ms = int((_time.monotonic() - start) * 1000)
        except Exception as exc:
            elapsed_ms = int((_time.monotonic() - start) * 1000)
            return ModelResponse(
                model=model, success=False, error=str(exc),
                total_duration_ms=elapsed_ms, provider=self.name,
            )

        if resp.status_code != 200:
            return ModelResponse(
                model=model, success=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                total_duration_ms=elapsed_ms, provider=self.name,
            )

        data = resp.json()
        message = data.get("message") or {}
        text = (message.get("content") or "").strip()
        tool_calls = self._extract_ollama_tool_calls(message)
        tokens_prompt = data.get("prompt_eval_count")
        tokens_completion = data.get("eval_count")
        return ModelResponse(
            text=text,
            model=model,
            success=True,
            total_duration_ms=elapsed_ms,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            provider=self.name,
            raw=data,
            tool_calls=tool_calls,
        )

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
        """Stream message-content deltas from /api/chat.

        Yields plain ``str`` chunks — the unified provider streaming shape.
        Non-200 responses raise :class:`ProviderUnavailableError` (with the
        upstream snippet so users can see what Ollama said). Malformed
        JSON lines are skipped silently — Ollama occasionally interleaves
        non-JSON heartbeat output and a single bad line shouldn't kill
        the stream.

        Retry + fallback are intentionally NOT applied here: a mid-stream
        failure would force a full re-run from the start, which is rarely
        what the caller wants. ``chat_completion`` keeps the retry path.
        """
        from app import model_router
        from app.config import settings

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        url = f"{settings.ollama_base_url}/api/chat"
        effective_timeout = (
            timeout if timeout != 600 else model_router._timeout_for(model)
        )
        client = model_router._get_client()

        async with client.stream(
            "POST", url, json=payload, timeout=effective_timeout,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                snippet = body[:200].decode("utf-8", errors="replace")
                raise ProviderUnavailableError(
                    f"ollama HTTP {resp.status_code}: {snippet}"
                )
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    # Heartbeat or partial frame — skip and keep streaming.
                    continue
                content = (chunk.get("message") or {}).get("content", "") or ""
                if content:
                    yield content
                if chunk.get("done"):
                    return


# Register the singleton at import time. ``app/providers/__init__.py``
# triggers this via its ``_autoload`` helper.
from app.providers import register  # noqa: E402

register("ollama", OllamaProvider())
