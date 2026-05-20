"""§17.195 — shared formatter for the orchestrator's ``next_actions`` field.

The orchestrator's ``app/modules/recovery.py`` is the single source of
truth for the next_actions REGISTRY (per-status recovery hints). This
module is the single source of truth for how that list RENDERS to a
human surface. Pre-§17.195 the OWUI pipeline (``pipelines/scaffold_
router.py``) and the CLI (``cli/scaffold_cli/main.py``) each kept their
own copy of the filter-then-format logic — same registry input rendered
differently and with diverging field selection per consumer.

The helpers below are deliberately small + style-agnostic so callers
can compose them into their own rendering pipeline:

  * ``filter_renderable(actions)`` — strip "wait" entries (noise on
    terminal/chat surfaces; only meaningful when something is in-flight).
  * ``action_clickable(action)`` — return ``(clickable_text, description)``
    for one action; ``clickable_text`` is the command (preferred), the
    method+endpoint pair, or ``None`` for description-only entries.
    Caller styles the clickable text (color, monospace, link) — the
    helper just decides WHICH field to surface.
  * ``format_block(actions, *, style)`` — full multi-line block for
    callers that don't care about per-token styling. ``style="markdown"``
    emits the OWUI pipeline's existing shape; ``style="plain"`` emits a
    terminal-friendly variant without markdown backticks.

This file is also vendored to ``pipelines/_next_actions.py`` (byte-equal
copy, see ``make sync-next-actions`` / ``make check-next-actions``) so
the OWUI pipelines container — which doesn't ship the SDK — has the
same helpers available.
"""
from __future__ import annotations

from typing import Iterable


# Action kinds that are noise on terminal/chat surfaces. "wait" is the
# only documented noise action today; future status flags ("queued",
# "delayed") would join this set if they need consumer-side filtering.
_NOISE_ACTIONS = frozenset({"wait"})


def filter_renderable(actions: Iterable[dict]) -> list[dict]:
    """Drop ``action == "wait"`` entries from a next_actions list.

    Pure function — does not mutate the input. Returns a new list (empty
    when every entry is noise, which is the common terminal-state case).
    """
    return [a for a in actions if a.get("action") not in _NOISE_ACTIONS]


def action_clickable(action: dict) -> tuple[str | None, str]:
    """Return ``(clickable_text, description)`` for one action.

    Preference order for ``clickable_text``: ``command`` (chat-form
    command, the most user-actionable surface) → ``method + endpoint``
    (REST equivalent for SDK/CLI users) → ``None`` (description-only
    action). The caller styles the clickable text — this helper just
    decides which field to surface and in what order.

    The description is always returned; if absent in the action dict,
    falls back to the empty string so callers can rely on a 2-tuple shape.
    """
    desc = action.get("description", "") or ""
    cmd = action.get("command")
    if cmd:
        return (cmd, desc)
    endpoint = action.get("endpoint")
    if endpoint:
        method = action.get("method") or "GET"
        return (f"{method} {endpoint}", desc)
    return (None, desc)


def format_block(actions: Iterable[dict], *, style: str = "markdown") -> str:
    """Format a next_actions list as a complete multi-line block.

    Returns ``""`` when every entry is noise (the common terminal-state
    case) so callers can no-op the inclusion without a separate empty
    check. ``style="markdown"`` matches the OWUI pipeline's pre-§17.195
    output byte-for-byte; ``style="plain"`` is the terminal variant
    (no backticks, 2-space indent, longer description separator for
    readability without color).

    Raises ``ValueError`` for an unknown style — fail loud rather than
    silently fall back to markdown.
    """
    renderable = filter_renderable(actions)
    if not renderable:
        return ""

    if style == "markdown":
        header = "**Next steps:**"
        bullet_with = "• `{clickable}` — {desc}"
        bullet_without = "• {desc}"
    elif style == "plain":
        header = "Next steps:"
        bullet_with = "  • {clickable}   — {desc}"
        bullet_without = "  • {desc}"
    else:
        raise ValueError(
            f"unknown style {style!r}; expected 'markdown' or 'plain'"
        )

    lines = ["", header]
    for a in renderable:
        clickable, desc = action_clickable(a)
        if clickable is not None:
            lines.append(bullet_with.format(clickable=clickable, desc=desc))
        else:
            lines.append(bullet_without.format(desc=desc))
    return "\n".join(lines)
