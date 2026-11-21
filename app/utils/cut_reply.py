"""§17.1986 — a structured reply cut at its token budget is never read as a whole object.

Live 2026-11-21 (llm_traces 13441/13442, the refine draw at max_tokens=2048): the coaxed reply stopped at the cap
mid-string, ``json_repair`` closed the open string, and the stump "(b) a summary you can click to" went to the
operator as a real ambiguity. Trace 13020 (an earlier refine) did the same to a constraint ("take a restorable copy
of any file before"). A JSON object parsed from a cut reply is complete in shape only: its LAST written value is
the one the cap fell in.

``model_router.tool_call`` and ``model_router.generate_json`` (the two parse points every structured caller goes
through) use this: a cut reply is redrawn once at a larger budget (``redraw_budget``); still cut, the incomplete
trailing item is dropped when the cut fell inside a list (``drop_cut_tail``), and the reply is refused — loudly —
when it fell in a scalar, where nothing whole is left to keep. §17.1986b — ``model_router.generate`` / ``chat`` take
``whole=True`` for a reply parsed or run as text (the ``generate``/``chat`` + ``parse_json_object`` callers, code, a
script): cut, it is redrawn once larger and, still cut, refused (``success=False``, ``cut_at``). §17.1984's ``idea_refinement`` guard is the same
rule held at one caller and can read ``reply_was_cut`` / ``drop_cut_tail`` from here.
"""
from __future__ import annotations

from typing import Any

# Tool-call ids the router gives to arguments it read out of reply TEXT (coaxed JSON §17.547, a call written as
# text §17.1827). Native tool calls are parsed strictly by the provider (Ollama emits only a complete call, OpenAI
# decodes with json.loads), so only text-read arguments can carry a repaired stump.
TEXT_READ_CALL_IDS = frozenset({"coaxed_0", "text_call_0"})


def reply_was_cut(resp: Any, budget: int | None = None) -> bool:
    """True when the reply stopped at its token limit, not at its end.

    The provider's own stop reason decides: Ollama ``done_reason='length'``, OpenAI
    ``choices[0].finish_reason='length'``, Anthropic ``stop_reason='max_tokens'``. A reply that names any other
    reason stopped normally. Only when no reason is known does the count decide (completion tokens ≥ ``budget``).
    Defensive on every field; never raises."""
    try:
        raw = getattr(resp, "raw", None) or {}
        reason = None
        if isinstance(raw, dict):
            reason = raw.get("done_reason") or raw.get("stop_reason")
            choices = raw.get("choices")
            if reason is None and isinstance(choices, list) and choices and isinstance(choices[0], dict):
                reason = choices[0].get("finish_reason")
        if reason:
            return str(reason) in ("length", "max_tokens")
        used = getattr(resp, "tokens_completion", None)
        return bool(budget) and isinstance(used, int) and used >= budget
    except Exception:
        return False


def redraw_budget(budget: int, used: int | None, cap: int) -> int:
    """The budget for the one redraw of a cut reply: twice what the cut draw actually had (a coax floor can have
    raised it above the caller's value), never above ``cap``. Equal to ``budget`` when there is no room left."""
    base = max(int(budget or 0), int(used or 0))
    return max(int(budget or 0), min(base * 2, int(cap)))


def drop_cut_tail(obj: Any) -> tuple[Any, Any]:
    """``(settled, dropped)`` for an object parsed from a cut reply, or ``(None, None)`` when nothing whole is left.

    The cap fell in the LAST value written (json_repair keeps the written order). A list loses its last item (an
    item cut before its end — a question without its options, a query without its object — is not an item); a
    dict recurses into its last value; a cut scalar, or a list cut before its first item, leaves nothing to keep.
    """
    if isinstance(obj, list):
        if not obj:
            return None, None
        return obj[:-1], obj[-1]
    if isinstance(obj, dict) and obj:
        key = next(reversed(obj))
        inner, dropped = drop_cut_tail(obj[key])
        if inner is None:
            return None, None
        return {**obj, key: inner}, dropped
    return None, None


def cut_at(resp: Any) -> int:
    """The budget a ``whole=True`` reply was refused at (``model_router._whole_reply``), else 0."""
    try:
        raw = getattr(resp, "raw", None) or {}
        return int(raw.get("cut_at") or 0) if isinstance(raw, dict) else 0
    except Exception:
        return 0
