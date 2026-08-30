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


def _thinking_squeezed(resp) -> bool:
    """§17.877 — non-empty but TRUNCATED because reasoning ate the budget.

    The §17.876 empty-content guard has a sibling failure the live HomeLab
    session exposed: the model thinks ~7k tokens, the answer STARTS, and the
    shared num_predict cap cuts it mid-command ("chown -R prowlarr:prowlarr
    /opt/Prowlarr /var/lib/prowl" — a fix the operator can't complete). The
    non-empty check passes, so no redraw or rescue fires and the operator gets
    a walkthrough missing its tail. Signature: Ollama ``done_reason ==
    "length"`` WITH a non-empty ``thinking`` field — a length-stop WITHOUT
    thinking is just a legitimately long answer and must be kept. Fail-soft:
    unknown/missing raw shapes (other providers, test mocks) → False."""
    raw = getattr(resp, "raw", None) or {}
    if not isinstance(raw, dict) or raw.get("done_reason") != "length":
        return False
    thinking = raw.get("thinking") or (raw.get("message") or {}).get("thinking") or ""
    return bool(str(thinking).strip())


async def _redraw_until(call, is_usable, *, draws, label, detail,
                        event="llm_empty_redraw", rescue=None):
    """§17.582 — core retry-on-empty loop shared by the three public guards.

    ``call`` is a zero-arg coroutine factory that performs one draw; ``is_usable``
    inspects the response and returns True when it's worth keeping. A hard
    failure (``success=False``) returns immediately; otherwise re-draw up to
    ``draws`` times, returning the last response so the caller's existing
    empty-handling still applies. Consolidates the previously-triplicated loop
    in ``generate_until_nonempty`` / ``chat_until_nonempty`` /
    ``tool_call_until_args`` (§17.464/465/581) so a future policy change (jitter,
    wall-time cap, metrics) lands in one place.

    §17.876 — ``rescue`` (optional zero-arg coroutine factory): one LAST-RESORT
    draw after all ``draws`` come back success-but-unusable. The live failure it
    exists for: at low temperature a thinking model's chain-of-thought is nearly
    DETERMINISTIC, so when reasoning alone overruns num_predict the redraws all
    fail identically ("variance lands non-empty on a fresh draw" — the §17.465
    premise — does not hold). The rescue changes the call (think=False) instead
    of re-rolling it. Used only if usable: an unusable/failed rescue returns the
    original last draw so caller error semantics are unchanged.
    """
    resp = None
    for d in range(draws):
        resp = await call()
        if not resp.success:
            return resp
        if is_usable(resp):
            return resp
        logger.warning(
            "%s: label=%s draw=%d/%d (%s)", event, label or "?", d + 1, draws, detail,
        )
    if rescue is not None:
        logger.warning(
            "llm_think_off_rescue: label=%s (all %d draws empty; final draw with "
            "think=False, §17.876)", label or "?", draws,
        )
        rescued = await rescue()
        if rescued.success and is_usable(rescued):
            return rescued
    return resp


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
    think_off_rescue: bool = False,
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
        think_off_rescue: §17.876 — after all ``draws`` come back empty, make
            ONE more draw with ``think=False`` (chain-of-thought disabled) so
            the whole token budget goes to content. Opt-in: the injected
            ``generate`` must accept ``think`` (``model_router.generate`` does;
            non-Ollama providers ignore it).

    Returns:
        The last response. A hard failure (``success=False``) returns
        immediately; otherwise the first non-empty draw, or the last empty one if
        all ``draws`` are exhausted (the caller then handles the empty as before).
    """
    return await _redraw_until(
        lambda: generate(
            prompt, system=system, temperature=temperature,
            max_tokens=max_tokens, **route_kwargs,
        ),
        lambda r: bool((r.text or "").strip()) and not (
            think_off_rescue and _thinking_squeezed(r)),
        draws=draws, label=label,
        detail="thinking-model empty/truncated content, §17.464/877",
        rescue=(lambda: generate(
            prompt, system=system, temperature=temperature,
            max_tokens=max_tokens, think=False, **route_kwargs,
        )) if think_off_rescue else None,
    )


async def chat_until_nonempty(
    chat,
    messages: list[dict],
    route_kwargs: dict,
    *,
    temperature: float,
    max_tokens: int,
    draws: int = 3,
    label: str = "",
    think_off_rescue: bool = False,
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
        think_off_rescue: §17.876 — after all ``draws`` come back empty, make
            ONE more draw with ``think=False``. The live incident this guards:
            a big fix prompt drove ~7.5k+ tokens of near-deterministic
            chain-of-thought past the 8192 budget on ALL draws (temp 0.3) →
            "(no fix returned)"; the same prompt with think=False answered in
            880 tokens. Opt-in: the injected ``chat`` must accept ``think``
            (``model_router.chat`` does; non-Ollama providers ignore it).

    Returns:
        The last response. A hard failure (``success=False``) returns
        immediately; otherwise the first non-empty draw, or the last empty one if
        all ``draws`` are exhausted (the caller handles the empty as before).
    """
    return await _redraw_until(
        lambda: chat(
            messages=messages, temperature=temperature,
            max_tokens=max_tokens, **route_kwargs,
        ),
        lambda r: bool((r.text or "").strip()) and not (
            think_off_rescue and _thinking_squeezed(r)),
        draws=draws, label=label,
        detail="thinking-model empty/truncated content, §17.465/877",
        rescue=(lambda: chat(
            messages=messages, temperature=temperature,
            max_tokens=max_tokens, think=False, **route_kwargs,
        )) if think_off_rescue else None,
    )

