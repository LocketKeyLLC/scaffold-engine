"""§17.811 — Shared progress + ETA estimator for long-running subsystems.

Long runs (DAG execution, research, RAG ingest, assist, decompose, EDA/sim)
previously surfaced only status transitions and a wall-clock "elapsed" marker —
no ETA, and no rolling summary of what the run is currently doing.
``ProgressTracker`` turns a stream of unit completions into a uniform snapshot
carrying an ETA (EWMA of per-unit durations) and a deterministic one-line
summary. Callers emit the snapshot as a ``progress`` SSE frame (see
``app.sse_events.PROGRESS``) and/or persist it to ``jobs.metadata.progress``.

Design notes
------------
- **Deterministic by default.** ``snapshot()["summary"]`` is templated; it never
  calls a model. ``narrate()`` is the *only* path that touches an LLM and is
  opt-in behind the ``progress_summary_llm_enabled`` valve — callers gate it.
- **Soft totals.** Research iterations can early-exit, so ``soft_total=True``
  marks the ETA as an upper bound (``eta_human`` rendered with a leading "≤").
- **Monotonic clock.** Uses ``time.monotonic()`` so it is immune to wall-clock
  jumps; ``clock=`` is injectable for deterministic tests.
- **Cold start.** ``eta_ms`` is ``None`` until at least one unit has completed
  (no per-unit rate yet, so no honest estimate).
- **Throttle helper.** ``EmitThrottle`` gates how often the live ``progress``
  frame and the ``jobs.metadata.progress`` persist actually fire, so a burst of
  fast nodes doesn't spam the SSE stream or the DB. It is per-run state (created
  once per execute call), so it works in both the serial and parallel-frontier
  executor paths without a global.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable


def humanize_ms(ms: int | float | None) -> str:
    """Render a millisecond duration as a compact ``"3m 20s"`` / ``"1h 04m"`` string."""
    if ms is None:
        return "unknown"
    total = max(0, int(ms // 1000))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class ProgressTracker:
    """Accumulates unit completions into an ETA + deterministic summary snapshot.

    Parameters
    ----------
    total:
        Number of units of work (nodes / iterations / entries / steps / stages).
        ``<= 0`` is tolerated (pct/eta degrade to ``None``) for callers that
        can't know the total up front.
    phase:
        Machine label for the subsystem/phase, e.g. ``"executing"`` /
        ``"researching"`` / ``"ingesting"``. Carried through to consumers.
    unit:
        Human noun for one unit, e.g. ``"steps"`` / ``"sources"`` / ``"docs"``.
    label:
        Optional human phase label, e.g. ``"Executing DAG"``.
    soft_total:
        When True the total is an upper bound (early-exit possible) and the ETA
        is presented as "≤".
    initial_completed:
        Units already terminal before this run started (a resumed DAG may have
        done/failed nodes). Counted toward ``completed``/``pct`` but NOT folded
        into the EWMA — their durations predate this run and would poison the
        rate estimate. ETA still cold-starts until the first *live* tick.
    alpha:
        EWMA smoothing factor for per-unit duration (higher = more reactive).
    clock:
        Monotonic clock source; injectable for tests.
    """

    def __init__(
        self,
        total: int,
        *,
        phase: str,
        unit: str = "steps",
        label: str | None = None,
        soft_total: bool = False,
        initial_completed: int = 0,
        alpha: float = 0.3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.total = max(0, int(total))
        self.phase = phase
        self.unit = unit
        self.label = label
        self.soft_total = soft_total
        self.alpha = min(max(float(alpha), 0.01), 1.0)
        self._clock = clock
        self._start = clock()
        self._last_tick_t = self._start
        self._completed = max(0, int(initial_completed))
        # §17.812 (audit M2) — the resume baseline: ETA is a wall-clock RATE over
        # work done THIS session (completed - initial) / elapsed, so a resumed run
        # doesn't divide pre-resume completions by only post-resume elapsed.
        self._initial = self._completed
        # Legacy per-unit EWMA — still folded by tick() but NO LONGER drives the
        # ETA (§17.812 switched eta_ms to an elapsed-rate model). Kept to avoid
        # churning tick()'s signature / the alpha knob; harmless.
        self._ewma_ms: float | None = None
        self.current_item: str | None = None
        self.done_items: list[str] = []

    # ------------------------------------------------------------------
    def tick(
        self,
        completed: int | None = None,
        *,
        current_item: str | None = None,
        done_items: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Record progress and return the current snapshot.

        ``completed`` is the cumulative count (defaults to previous + 1). Only a
        forward delta folds into the EWMA — a repeated/stale count updates the
        item labels without perturbing the rate estimate.
        """
        now = self._clock()
        if completed is None:
            completed = self._completed + 1
        completed = max(0, int(completed))

        delta_units = completed - self._completed
        if delta_units > 0:
            span = now - self._last_tick_t
            per_unit = (span / delta_units) * 1000.0
            self._ewma_ms = (
                per_unit
                if self._ewma_ms is None
                else (self.alpha * per_unit + (1.0 - self.alpha) * self._ewma_ms)
            )
            self._last_tick_t = now
            self._completed = completed

        if current_item is not None:
            self.current_item = current_item
        if done_items is not None:
            self.done_items = list(done_items)
        return self.snapshot(now=now)

    # ------------------------------------------------------------------
    def eta_ms(self, now: float | None = None) -> int | None:
        """Estimated remaining time in ms, or ``None`` before any forward progress.

        §17.812 (audit M2) — elapsed-RATE model, not a per-unit EWMA. The old
        EWMA folded the wall-clock span between successive ``tick()`` calls into a
        "per-node duration"; under the parallel frontier ``tick()`` fires once per
        result drained from the queue, so that span was the inter-ARRIVAL gap
        (near-zero within a wave), which dragged the estimate far below the truth.

        Wall-clock per completed unit = elapsed / units_done_this_session already
        accounts for concurrency: C nodes finishing in wall-time D give D/C per
        unit, so ETA = (D/C) × remaining is correct. Collapses to mean node
        duration × remaining for a serial run.
        """
        if self.total <= 0:
            return None
        remaining = max(0, self.total - self._completed)
        if remaining == 0:
            return 0  # complete — no time left
        now = now if now is not None else self._clock()
        done = self._completed - self._initial
        elapsed = now - self._start
        if done <= 0 or elapsed <= 0:
            return None  # no forward progress this session yet
        per_unit_wall_s = elapsed / done
        return int(per_unit_wall_s * remaining * 1000.0)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Return the current progress snapshot (the payload emitted/persisted)."""
        now = now if now is not None else self._clock()
        elapsed_ms = int((now - self._start) * 1000)
        eta = self.eta_ms(now)
        pct = (
            int(round(100.0 * self._completed / self.total)) if self.total > 0 else None
        )
        rate = None
        if elapsed_ms > 0 and self._completed > 0:
            rate = round(self._completed / (elapsed_ms / 60000.0), 2)
        eta_human = None
        if eta is not None:
            eta_human = ("≤ " if self.soft_total else "~") + humanize_ms(eta)

        snap: dict[str, Any] = {
            "phase": self.phase,
            "label": self.label,
            "unit": self.unit,
            "completed": self._completed,
            "total": self.total,
            "pct": pct,
            "elapsed_ms": elapsed_ms,
            "eta_ms": eta,
            "eta_human": eta_human,
            "rate_per_min": rate,
            "current_item": self.current_item,
            "done_items": list(self.done_items),
            "soft": self.soft_total,
        }
        snap["summary"] = self._summary(snap)
        return snap

    # ------------------------------------------------------------------
    def _summary(self, snap: dict[str, Any]) -> str:
        """Deterministic one-line summary, e.g. ``"4/10 nodes · 40% · ~3m 20s left"``."""
        total = snap["total"] or "?"
        parts = [f"{snap['completed']}/{total} {self.unit}"]
        if snap["pct"] is not None:
            parts.append(f"{snap['pct']}%")
        if snap["eta_human"]:
            parts.append(f"{snap['eta_human']} left")
        return " · ".join(parts)

    # ------------------------------------------------------------------
    async def narrate(self, model_router: Any, *, role: str = "model_general") -> str | None:
        """Opt-in one-line prose summary via ``role`` (default general model).

        The *only* LLM-touching method. Callers MUST gate this on the
        ``progress_summary_llm_enabled`` valve — it is never invoked in the
        default deterministic path. Fail-soft: returns ``None`` on any error so a
        narration hiccup can't break a run.
        """
        try:
            snap = self.snapshot()
            done = ", ".join(snap["done_items"][-6:]) or "nothing yet"
            prompt = (
                "You narrate live progress of a long automated run in ONE short "
                "sentence (max 20 words), present tense, no preamble.\n"
                f"Phase: {snap['label'] or snap['phase']}\n"
                f"Done ({snap['completed']}/{snap['total']}): {done}\n"
                f"Now: {snap['current_item'] or 'n/a'}\n"
                f"ETA: {snap['eta_human'] or 'unknown'}\n"
                "Sentence:"
            )
            resp = await model_router.generate(
                prompt, role=role, temperature=0.3, max_tokens=80
            )
            # §17.854 (audit B5) — ModelResponse exposes `.text`, not `.content`;
            # the old attribute meant this (currently caller-less) narrator would
            # always return None even if wired. Fixed so the valve isn't a trap.
            text = (getattr(resp, "text", None) or "").strip()
            try:
                from app.utils.llm_parsing import strip_think_tags

                text = strip_think_tags(text).strip()
            except Exception:
                pass
            return text.splitlines()[0].strip() if text else None
        except Exception:
            return None


class EmitThrottle:
    """Rate-limits how often the live ``progress`` frame + persist actually fire.

    Per-run state (one instance per execute call) so it needs no global and works
    identically in the serial and parallel-frontier executor paths. ``ready()``
    returns True at most once per ``min_interval`` seconds, EXCEPT it always
    returns True for a ``final=True`` call (the terminal snapshot must always
    land) and for the very first call (so progress shows up promptly).
    """

    def __init__(
        self, min_interval: float = 5.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._last: float | None = None

    def ready(self, *, final: bool = False) -> bool:
        now = self._clock()
        if final or self._last is None or (now - self._last) >= self.min_interval:
            self._last = now
            return True
        return False
