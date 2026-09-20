"""§17.1111 (Phase 1 ledger L-3) — adaptive think-off for budget-starving models.

A reasoning model's ``num_predict`` is a SHARED thinking+content budget. The
ledger measured ``deepseek-v4-pro:cloud`` (model_general) landing EXACTLY on
its budget in 13 % of calls (1024/2048/2500/3000/8192 — every caller's cap)
with nothing usable back: ``done_reason='length'``, empty or truncated
content. Each such draw is 10–25 s of wall time for nothing. The §17.1053
tool-call rescue and the §17.876 generate/chat rescue already switch to
``think=False`` — but only AFTER a starved draw, on that one call, so every
call pays the wasted draw again. 116 capped calls on 2026-09-18 alone.

This module remembers. ``note_starved_draw(model)`` records the event;
``think_default(model)`` answers ``False`` once a model has starved
``settings.think_off_after_starved_draws`` times inside
``settings.think_off_window_minutes``, and ``None`` (model default) otherwise.
Callers that pass an explicit ``think`` are never overridden; the policy fills
in only when the caller left it to the model. The window decays, so a model
that stops starving (a tuning change, a different model behind the role) gets
its chain-of-thought back without a restart. Pure and in-process: no I/O, no
app imports beyond settings.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from app.config import settings

logger = logging.getLogger("scaffold.think_policy")

_LOCK = threading.Lock()
_STARVED: dict[str, deque[float]] = {}
_FLIPPED: dict[str, float] = {}       # model → when think-off first took effect


def _threshold() -> int:
    return max(1, int(getattr(settings, "think_off_after_starved_draws", 2) or 2))


def _window_s() -> float:
    return max(60.0, float(getattr(settings, "think_off_window_minutes", 60) or 60) * 60.0)


def _prune(model: str, now: float) -> deque[float]:
    q = _STARVED.setdefault(model, deque())
    cutoff = now - _window_s()
    while q and q[0] < cutoff:
        q.popleft()
    return q


def enabled() -> bool:
    return bool(getattr(settings, "think_off_adaptive_enabled", True))


def note_starved_draw(model: str | None, *, where: str = "", budget: int | None = None) -> bool:
    """Record one starved draw for ``model``. Returns True when this event is
    the one that flips the model to think-off."""
    if not enabled() or not (model or "").strip():
        return False
    model = model.strip()
    now = time.monotonic()
    with _LOCK:
        q = _prune(model, now)
        q.append(now)
        n = len(q)
        flipped_now = n >= _threshold() and model not in _FLIPPED
        if flipped_now:
            _FLIPPED[model] = now
    if flipped_now:
        logger.warning(
            "think_policy_flipped: model=%s think=False starved_draws=%d window_min=%d "
            "where=%s budget=%s (§17.1111 — reasoning was eating the budget; callers "
            "that leave think unset now get think=False on the FIRST draw)",
            model, n, int(_window_s() // 60), where or "-", budget if budget is not None else "-",
        )
    else:
        logger.info("think_policy_starved_draw: model=%s count=%d/%d where=%s budget=%s",
                    model, n, _threshold(), where or "-", budget if budget is not None else "-")
    return flipped_now


def think_default(model: str | None) -> bool | None:
    """``False`` while ``model`` is inside its think-off window, else ``None``."""
    if not enabled() or not (model or "").strip():
        return None
    model = model.strip()
    now = time.monotonic()
    with _LOCK:
        q = _prune(model, now)
        if len(q) >= _threshold():
            return False
        if model in _FLIPPED:
            # the window drained — give the chain-of-thought back, say so once
            del _FLIPPED[model]
            restored = True
        else:
            restored = False
    if restored:
        logger.info("think_policy_restored: model=%s (no starved draws in the last %d min)",
                    model, int(_window_s() // 60))
    return None


def resolve(model: str | None, think: bool | None) -> bool | None:
    """The value to send: the caller's explicit choice wins; ``None`` asks the
    policy."""
    return think if think is not None else think_default(model)


def is_starved(resp: Any) -> bool:
    """A draw that hit the token limit and came back with nothing usable — no
    tool call, empty content — OR (§17.877) truncated content with a
    non-empty ``thinking`` field (reasoning ran past the budget). Defensive on
    every field; never raises."""
    try:
        raw = getattr(resp, "raw", None) or {}
        if not isinstance(raw, dict) or raw.get("done_reason") != "length":
            return False
        msg = raw.get("message") or {}
        if msg.get("tool_calls"):
            return False
        content = (msg.get("content") or "") or (getattr(resp, "text", "") or "")
        if not str(content).strip():
            return True
        thinking = raw.get("thinking") or msg.get("thinking") or ""
        return bool(str(thinking).strip())
    except Exception:
        return False


def state() -> dict[str, Any]:
    """Snapshot for logs/health: models currently forced to think=False."""
    now = time.monotonic()
    with _LOCK:
        out = {}
        for m in list(_STARVED):
            q = _prune(m, now)
            if q:
                out[m] = {"starved_draws": len(q), "think_off": len(q) >= _threshold()}
    return {"enabled": enabled(), "threshold": _threshold(),
            "window_minutes": int(_window_s() // 60), "models": out}


def reset() -> None:
    """Test hook."""
    with _LOCK:
        _STARVED.clear()
        _FLIPPED.clear()
