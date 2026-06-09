"""§17.464 — shared retry-on-empty guard for free-text LLM calls.

Thinking models (``qwen3.5:397b-cloud`` is the default for most roles since the
§17.440 cloud migration) can spend their token budget on reasoning and return
``success=True`` with EMPTY content. Every consumer that *parses* a
``model_router.generate`` result — ``parse_json_object`` / ``parse_json_array``,
or treats the text as the deliverable — silently degrades or hard-fails on that
empty draw. This was hit three times in different paths before it was named:
§17.453 (CoVe), §17.462 (prompt optimizer), §17.463 (DAG generation).

``generate_until_nonempty`` centralises the guard so the *next* consumer can't
reintroduce the bug: it re-draws on a success+empty response (the thinking
variance almost always lands non-empty on a fresh draw) and surfaces hard
failures (``success=False``) immediately so the caller's existing error handling
still applies. Callers parse the returned response as before.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("scaffold.llm_retry")


async def generate_until_nonempty(
    generate,
    prompt: str,
    route_kwargs: dict,
    *,
    system: str,
    temperature: float,
    max_tokens: int,
    draws: int = 3,
    label: str = "",
):
    """Call ``generate``, re-drawing on a success+empty response.

    Args:
        generate: The ``model_router.generate`` coroutine, passed in by the
            caller (dependency injection) so the caller's own — and crucially,
            test-patched — reference is used. ``model_router`` is commonly
            ``MagicMock``-replaced at the caller's module level in tests, which a
            helper-internal ``from app import model_router`` would miss.
        prompt: User prompt.
        route_kwargs: Routing kwargs forwarded verbatim — e.g. ``{"role": ...,
            "overrides": ...}`` or ``{"model": ...}``. Keeps the caller's existing
            role/model selection intact.
        system, temperature, max_tokens: Standard generate params. Pass a
            generous ``max_tokens`` (8192) for thinking models — a tight budget
            is what makes them overrun into empty content.
        draws: Max independent attempts (default 3).
        label: Short tag for the redraw warning log line.

    Returns:
        The last response. A hard failure (``success=False``) returns
        immediately; otherwise the first non-empty draw, or the last empty one if
        all ``draws`` are exhausted (the caller then handles the empty as before).
    """
    resp = None
    for d in range(draws):
        resp = await generate(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            **route_kwargs,
        )
        if not resp.success:
            return resp
        if (resp.text or "").strip():
            return resp
        logger.warning(
            "llm_empty_redraw: label=%s draw=%d/%d (thinking-model empty content, §17.464)",
            label or "?", d + 1, draws,
        )
    return resp


async def chat_until_nonempty(
    chat,
    messages: list[dict],
    route_kwargs: dict,
    *,
    temperature: float,
    max_tokens: int,
    draws: int = 3,
    label: str = "",
):
    """``chat()`` variant of :func:`generate_until_nonempty` (§17.465).

    Same retry-on-empty semantics, for callers that build an explicit
    ``messages`` list (system + user) rather than the ``prompt`` + ``system``
    shape ``generate`` takes — e.g. the node executor, which selects a
    tool-specific system prompt (``_system_for_tool``) and must keep the
    message structure intact.

    Why a sibling rather than reusing ``generate_until_nonempty``: switching the
    executor from ``/api/chat`` to ``/api/generate`` to fit the existing helper
    would change the wire endpoint for every node call — a behavioral change far
    larger than the empty-guard it buys. This keeps the executor on ``chat``.

    Args:
        chat: The ``model_router.chat`` coroutine, dependency-injected so the
            caller's (and test-patched) reference is used — see the
            ``generate_until_nonempty`` docstring for why injection matters.
        messages: The full message list, forwarded verbatim each draw.
        route_kwargs: Routing kwargs forwarded verbatim — e.g. ``{"role": ...,
            "overrides": ...}`` or ``{"model": ...}``.
        temperature, max_tokens: Standard chat params. Pass a generous
            ``max_tokens`` (8192) for thinking models — a tight budget is what
            makes them overrun reasoning into empty/truncated content.
        draws: Max independent attempts (default 3).
        label: Short tag for the redraw warning log line.

    Returns:
        The last response. A hard failure (``success=False``) returns
        immediately; otherwise the first non-empty draw, or the last empty one if
        all ``draws`` are exhausted (the caller handles the empty as before).
    """
    resp = None
    for d in range(draws):
        resp = await chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **route_kwargs,
        )
        if not resp.success:
            return resp
        if (resp.text or "").strip():
            return resp
        logger.warning(
            "llm_empty_redraw: label=%s draw=%d/%d (thinking-model empty content, §17.465)",
            label or "?", d + 1, draws,
        )
    return resp
