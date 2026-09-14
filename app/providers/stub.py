"""§17.1061 — the stub provider: canned, instant, deterministic answers.

Registered as ``"stub"`` so any role can be pointed at it with
``MODEL_<ROLE>_PROVIDER=stub``. It exists for two things a real backend
cannot give: a load profile that exercises an LLM path (``make load-llm``)
without spending a token or a GPU second, and an offline lane where the
question is "does the engine's own plumbing hold", not "is the model good".
Never a default; never registered for the embedder role (no vectors).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from app.providers import register
from app.providers.base import LLMProvider, ModelResponse, Tool, ToolCall

_CANNED = (
    "STUB: this is a canned answer from the stub provider. "
    "It carries no knowledge; it exists to exercise the engine's own paths."
)


def _example_for(schema: dict[str, Any]) -> Any:
    """A minimal value satisfying a JSON-schema fragment (required keys only)."""
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        props = schema.get("properties") or {}
        return {k: _example_for(props.get(k) or {}) for k in schema.get("required") or []}
    if t == "array":
        return [_example_for(schema.get("items") or {})] if schema.get("minItems", 0) else []
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    return "stub"


class StubProvider(LLMProvider):
    name = "stub"
    supports_chat = True
    supports_embeddings = False
    supports_streaming = True
    supports_native_tools = True
    supports_structured_outputs = False

    #: simulated latency per call — small but non-zero so concurrency shows
    latency_s: float = 0.02

    async def chat_completion(self, model: str, messages: list[dict[str, str]], *,
                              temperature: float = 0.7, max_tokens: int = 4096,
                              timeout: int = 600, **opts: Any) -> ModelResponse:
        await asyncio.sleep(self.latency_s)
        last = (messages[-1].get("content") if messages else "") or ""
        return ModelResponse(text=f"{_CANNED} (echo: {last[:80]})", model=model, success=True,
                             provider=self.name, tokens_prompt=len(last) // 4, tokens_completion=32,
                             raw={"done_reason": "stop", "done": True, "message": {"content": _CANNED}})

    async def generate(self, model: str, prompt: str, *, system: str = "", temperature: float = 0.7,
                       max_tokens: int = 4096, timeout: int = 600, **opts: Any) -> ModelResponse:
        return await self.chat_completion(model, [{"role": "user", "content": prompt}],
                                          temperature=temperature, max_tokens=max_tokens, timeout=timeout)

    async def stream_chat(self, model: str, messages: list[dict[str, str]], *, temperature: float = 0.7,
                          max_tokens: int = 4096, timeout: int = 600, **opts: Any) -> AsyncIterator[str]:
        for word in _CANNED.split(" "):
            await asyncio.sleep(self.latency_s / 10)
            yield word + " "

    async def tool_call(self, model: str, messages: list[dict[str, str]], tools: list[Tool], *,
                        temperature: float = 0.7, max_tokens: int = 4096, timeout: int = 600,
                        tool_choice: str = "auto", **opts: Any) -> ModelResponse:
        await asyncio.sleep(self.latency_s)
        if not tools:
            return await self.chat_completion(model, messages)
        t = tools[0]
        args = _example_for(t.input_schema or {"type": "object"})
        return ModelResponse(text="", model=model, success=True, provider=self.name,
                             tool_calls=[ToolCall(id="stub_0", name=t.name, arguments=args)],
                             raw={"done_reason": "stop", "done": True,
                                  "message": {"content": "", "tool_calls": [{"function": {"name": t.name, "arguments": args}}]}})

    async def list_models(self) -> list[str]:
        return ["stub"]

    async def health_check(self) -> dict[str, Any]:
        return {"status": "up", "provider": self.name, "models": ["stub"]}


register("stub", StubProvider())
