"""§17.1109 — per-stage timing for one assist turn (Phase 1 ledger L-1).

The ledger measured assist turns at p50 74 s / p90 184 s with **31 s per turn
outside any model call** and nothing durable saying where: the stored frames
carried no timestamps, ``llm_call_logs.call_kind`` was empty for every real
call, and ``llm_traces`` was off. This module is the attribution:

* ``TurnTimer`` — the turn driver marks a stage at every operator-facing
  status frame ("Reading that…", "Deciding how to act on that…", …), so the
  stage names ARE the ones the operator sees. Each stage accrues wall time and
  the LLM time/calls that landed inside it; ``finish()`` returns the record
  the driver writes to ``assist_turn_runs.timings`` and logs.
* ``current_turn_timer`` — a ContextVar the driver sets for the turn's task;
  ``cost_tracking.record_llm_call`` reads it to (a) accrue the call to the
  current stage and (b) default ``call_kind`` to ``assist:<stage>`` when the
  caller set none — so the call log finally says which stage paid.

Pure: no I/O, no app imports (``cost_tracking`` imports this, not the reverse).
"""
from __future__ import annotations

import re
import time
from contextvars import ContextVar
from typing import Any, Optional

current_turn_timer: ContextVar[Optional["TurnTimer"]] = ContextVar(
    "scaffold_current_turn_timer", default=None,
)

_LABEL_MAX = 60
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(label: str) -> str:
    """'Deciding how to act on that…' → 'deciding_how_to_act_on_that'."""
    return _SLUG_RE.sub("_", (label or "").lower()).strip("_")[:48] or "stage"


class TurnTimer:
    """Wall-clock stages for one turn. ``mark`` closes the open stage and opens
    the next; ``note_llm`` accrues a model call to the open stage."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self.t0 = clock()
        self.stages: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None
        self.llm_ms = 0
        self.llm_calls = 0
        self.annotations: dict[str, Any] = {}
        self.mark("start")

    # -- clock -------------------------------------------------------------
    def elapsed_ms(self) -> int:
        return int((self._clock() - self.t0) * 1000)

    # -- stages ------------------------------------------------------------
    def _close(self) -> None:
        if self._open is None:
            return
        now = self._clock()
        self._open["ms"] = int((now - self._open.pop("_t")) * 1000)
        self.stages.append(self._open)
        self._open = None

    def mark(self, label: str | None) -> None:
        self._close()
        self._open = {
            "label": (label or "").strip()[:_LABEL_MAX] or "stage",
            "at_ms": self.elapsed_ms(),
            "llm_ms": 0,
            "llm_calls": 0,
            "_t": self._clock(),
        }

    @property
    def current_label(self) -> str | None:
        return self._open["label"] if self._open else None

    def annotate(self, key: str, value: Any) -> None:
        self.annotations[key] = value

    # -- llm ---------------------------------------------------------------
    def note_llm(self, latency_ms: int) -> None:
        ms = max(0, int(latency_ms or 0))
        self.llm_ms += ms
        self.llm_calls += 1
        if self._open is not None:
            self._open["llm_ms"] += ms
            self._open["llm_calls"] += 1

    # -- result ------------------------------------------------------------
    def finish(self) -> dict[str, Any]:
        self._close()
        total = self.elapsed_ms()
        return {
            "total_ms": total,
            "llm_ms": self.llm_ms,
            "llm_calls": self.llm_calls,
            "non_llm_ms": max(0, total - self.llm_ms),
            "stages": [
                {**s, "non_llm_ms": max(0, s["ms"] - s["llm_ms"])}
                for s in self.stages
            ],
            **({"annotations": self.annotations} if self.annotations else {}),
        }

    def summary_line(self, result: dict[str, Any]) -> str:
        """One log line: total, llm share, and each stage as label:ms/llm."""
        parts = ",".join(
            f"{slug(s['label'])}:{s['ms']}/{s['llm_ms']}" for s in result["stages"]
        )
        return (
            f"total_ms={result['total_ms']} llm_ms={result['llm_ms']} "
            f"llm_calls={result['llm_calls']} non_llm_ms={result['non_llm_ms']} "
            f"stages(ms/llm_ms)={parts}"
        )


# -- hooks for the call recorder ----------------------------------------------

def note_llm_call(latency_ms: int) -> None:
    """Accrue one completed model call to the active turn (no-op outside one)."""
    t = current_turn_timer.get()
    if t is not None:
        t.note_llm(latency_ms)


def default_call_kind() -> str | None:
    """``assist:<stage slug>`` while a turn is active, else None."""
    t = current_turn_timer.get()
    if t is None or not t.current_label:
        return None
    return f"assist:{slug(t.current_label)}"
