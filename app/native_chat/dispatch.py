"""Turn dispatcher for the native OpenAI surface (§17.790).

:func:`route` reproduces the load-bearing precedence from the OWUI pipeline's
``pipe()`` for the command layer: a pending confirm-card follow-up wins first,
then NL command classification. It returns an async generator of text pieces when
it handles the turn, or ``None`` to fall through to the model passthrough (which
Phase 3 replaces with conversational triage). OWUI background task-calls (title /
tag generation) short-circuit straight to the passthrough — they are not operator
commands.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from app.native_chat import confirm_cards, nl_commands


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _looks_like_owui_task(text: str) -> bool:
    """OWUI sends background prompts (chat title, tags, follow-ups) as chat
    completions using a ``### Task:`` template — those are not operator commands."""
    return "### Task:" in text


async def _say(text: str) -> AsyncIterator[str]:
    yield text


async def route(messages: list[dict[str, Any]]) -> AsyncIterator[str] | None:
    """Route a turn through the command layer, or return None to fall through."""
    user_text = _last_user_text(messages)
    if not user_text.strip() or _looks_like_owui_task(user_text):
        return None

    # (a) Pending confirm-card follow-up — an affirmative commits the stored
    # action; a negative cancels; anything else re-classifies as a fresh turn.
    pending = confirm_cards.extract_pending(messages)
    if pending is not None:
        if confirm_cards.is_affirmative(user_text):
            gen = nl_commands.commit(pending)
            if gen is not None:
                return gen
        elif confirm_cards.is_negative(user_text):
            return _say("Cancelled — nothing was changed.")
        # else: fall through to fresh classification

    # (b) NL command classification (None when not a high-confidence command).
    return await nl_commands.classify_and_dispatch(user_text)


# Backwards-friendly alias for the package export.
run_turn = route
