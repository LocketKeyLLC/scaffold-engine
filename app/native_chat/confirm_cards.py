"""Confirm-card lifecycle for native NL commands (§17.790).

Expensive/destructive intents don't fire on classification — they emit a confirm
card and commit only on an explicit affirmative follow-up. State is carried in
the chat transcript itself (stateless, like the OWUI pipeline): the assistant
message embeds a marker

    [nlc]: NL_CONFIRM:<base64url(json({intent, slots}))>

which reconstructs the pending action from the incoming ``messages[]`` on the
next turn. The marker reuses the pipeline's ``§17.660`` format (a markdown
reference-link definition — renders as nothing, survives history replay). The
OpenAI surface has no rendering quirk, but keeping one format means one mental
model across both entry points.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

_MARKER_PREFIX = "NL_CONFIRM:"
# Matches the reference-link form the card emits, plus the legacy HTML-comment
# form for cross-compat with anything that still writes it.
_MARKER_RE = re.compile(r"(?:\[nlc\]:\s*|<!--\s*)NL_CONFIRM:([A-Za-z0-9_-]+)")

# First-word affirmatives / negatives. A pending confirm commits only on a clear
# yes; a clear no cancels; anything else re-classifies as a fresh turn.
_AFFIRMATIVE = frozenset({
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
    "do", "go", "proceed", "please", "affirmative", "correct", "run", "send", "yes.",
})
_NEGATIVE = frozenset({
    "no", "n", "nope", "nah", "cancel", "stop", "abort", "don't", "dont", "never",
    "negative", "no.",
})


def encode(intent: str, slots: dict[str, Any]) -> str:
    """Encode a pending action into the base64url marker token."""
    payload = json.dumps({"intent": intent, "slots": slots}, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return b64


def render_card(intent: str, slots: dict[str, Any], human_text: str) -> str:
    """A confirm card: the human prompt plus the hidden marker line."""
    token = encode(intent, slots)
    return f"{human_text}\n\n[nlc]: {_MARKER_PREFIX}{token}"


def extract_pending(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reconstruct a pending action from the most recent assistant message.

    Only the immediately-preceding assistant turn counts (a stale card earlier
    in the history must not re-arm). Returns ``{"intent", "slots"}`` or None.
    """
    last_assistant = None
    for m in reversed(messages):
        if m.get("role") == "assistant":
            last_assistant = m
            break
        if m.get("role") == "user":
            # a user message sits between us and any assistant card → the card,
            # if any, is the one just before this user turn; keep scanning back
            # for the assistant that produced it.
            continue
    if not last_assistant:
        return None
    match = _MARKER_RE.search(str(last_assistant.get("content") or ""))
    if not match:
        return None
    token = match.group(1)
    padding = "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))
    except Exception:
        return None
    intent = payload.get("intent")
    if not intent:
        return None
    return {"intent": intent, "slots": payload.get("slots") or {}}


def _first_word(text: str) -> str:
    stripped = (text or "").strip().lower()
    if not stripped:
        return ""
    return re.split(r"[\s,.!?]+", stripped, maxsplit=1)[0]


def is_affirmative(text: str) -> bool:
    return _first_word(text) in _AFFIRMATIVE


def is_negative(text: str) -> bool:
    return _first_word(text) in _NEGATIVE


def strip_marker(text: str) -> str:
    """Remove any confirm marker line from text (for display/echo)."""
    return _MARKER_RE.sub("", text).rstrip()
