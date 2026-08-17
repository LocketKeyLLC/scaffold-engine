"""OpenAI-compatible wire types for the native ``/v1`` surface (§17.788).

Pydantic models for the inbound OpenAI chat protocol plus small builders for the
outbound ``chat.completion`` / ``chat.completion.chunk`` shapes. Kept separate
from ``app/schemas.py`` (the engine's own request/response models) so the
OpenAI-shaped contract is isolated and the main OpenAPI snapshot is untouched —
the ``/v1`` sub-app owns these.

Only the fields the engine actually uses are typed; unknown fields OpenAI clients
send (``n``, ``presence_penalty``, ``user``, ``seed``, …) are tolerated via
``extra="allow"`` rather than rejected, so a stock client never 422s on a benign
extra field.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── Inbound ───────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    """One OpenAI chat message. ``content`` may be a plain string or the
    multimodal content-parts list; :func:`message_text` flattens it to text."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[Any] | None = None
    name: str | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    """Inbound ``POST /v1/chat/completions`` body (subset of the OpenAI schema)."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    # Modern OpenAI clients (and reasoning models) send max_completion_tokens.
    max_completion_tokens: int | None = None
    stream_options: StreamOptions | None = None

    def resolved_max_tokens(self, default: int) -> int:
        """Prefer max_completion_tokens, then max_tokens, then the default."""
        return self.max_completion_tokens or self.max_tokens or default

    def resolved_temperature(self, default: float) -> float:
        return default if self.temperature is None else self.temperature


# ── Model listing ─────────────────────────────────────────────────────────────
class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "scaffold-engine"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


def model_list(model_id: str) -> ModelList:
    return ModelList(data=[ModelCard(id=model_id, created=int(time.time()))])


# ── Helpers ───────────────────────────────────────────────────────────────────
def message_text(content: str | list[Any] | None) -> str:
    """Flatten OpenAI message content to plain text.

    A string passes through; a content-parts list keeps only ``text`` parts
    (image/audio parts are dropped — the engine is text-native today)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        elif isinstance(part, str):
            parts.append(part)
    return "".join(parts)


def to_router_messages(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Convert inbound OpenAI messages to the ``{role, content}`` dicts the
    model_router / providers expect, flattening multimodal content to text."""
    return [{"role": m.role, "content": message_text(m.content)} for m in messages]


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


# ── Outbound builders (plain dicts — fast to json.dumps for the SSE path) ──────
def completion_response(
    *,
    completion_id: str,
    model: str,
    content: str,
    finish_reason: str = "stop",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> dict[str, Any]:
    """A non-streaming ``chat.completion`` object."""
    pt = prompt_tokens or 0
    ct = completion_tokens or 0
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


def chunk(
    *,
    completion_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """A single ``chat.completion.chunk`` object for the SSE stream."""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }
