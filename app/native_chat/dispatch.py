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

import re
from typing import Any, AsyncIterator

from app.native_chat import confirm_cards, nl_commands, triage

_SLASH_RE = re.compile(r"^\s*/(\w+)\b", re.S)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _looks_like_owui_task(text: str) -> bool:
    """OWUI sends background prompts (chat title, tags, follow-ups) as chat
    completions using a ``### Task:`` template — those are not operator commands."""
    return "### Task:" in text


def _slash_verb(text: str) -> str | None:
    m = _SLASH_RE.match(text or "")
    return m.group(1).lower() if m else None


async def _say(text: str) -> AsyncIterator[str]:
    yield text


async def route(messages: list[dict[str, Any]]) -> AsyncIterator[str] | None:
    """Route a turn: a handled turn returns an async text generator; ``None``
    means "not an operator turn" → the raw model passthrough (OWUI task-calls).

    Precedence mirrors the pipeline's ``pipe()`` for the layers implemented so
    far: OWUI task-call short-circuit → ``/go`` → pending confirm-card → NL
    command → conversational triage (the default for any plain message)."""
    user_text = _last_user_text(messages)
    if not user_text.strip() or _looks_like_owui_task(user_text):
        return None

    # (0) Slash commands. /go|/run synthesize + submit Phase 1 (§17.791); the
    # /confirm auto-chain is Phase 3b — other slashes fall through for now.
    verb = _slash_verb(user_text)
    if verb in ("go", "run"):
        return triage.run_go(messages)

    # (a) Pending confirm-card follow-up — affirmative commits, negative cancels,
    # anything else re-classifies.
    pending = confirm_cards.extract_pending(messages)
    if pending is not None:
        if confirm_cards.is_affirmative(user_text):
            gen = nl_commands.commit(pending)
            if gen is not None:
                return gen
        elif confirm_cards.is_negative(user_text):
            return _say("Cancelled — nothing was changed.")

    # (b) NL command classification (None when not a high-confidence command).
    gen = await nl_commands.classify_and_dispatch(user_text)
    if gen is not None:
        return gen

    # (c) Conversational triage — the default for any plain message (was the raw
    # model passthrough in Phase 2; Phase 3a makes the engine scope the idea).
    return triage.run_triage(messages)


# Backwards-friendly alias for the package export.
run_turn = route
