"""§17.1007 — detach a run from the HTTP response that started it.

The problem
-----------
``POST /execute/all`` returned ``StreamingResponse(execute_all_nodes(...))``, so
the SSE response *was* the run: Starlette cancels the generator when the client
goes away, ``execute_all_nodes``'s ``finally`` sees ``CancelledError`` and marks
the job cancelled. That is deliberate and it is documented — and it means a
10–25 minute autonomous run is hostage to a browser tab.

The console did what it could with that: a ``beforeunload`` guard, a nav guard
on every hub tab, a confirm dialog. Every one of those is a correct response to
the constraint, and none of them addresses it. Past roughly ten seconds
attention leaves a task and does not come back on its own, so the design
contract has to change from "wait" to "go away, we will tell you" — which is
impossible while leaving destroys the work. The console ends up asking for
twenty minutes of sustained monitoring of a process that almost never needs
intervention, and the operator looks away regardless.

What this does
--------------
The run becomes a background task that owns the generator; the HTTP response
becomes a *subscriber* to its output. Disconnecting drops the subscriber and
leaves the task running. Reconnecting attaches a new subscriber to the same
task — the theater already re-seeds durable node state from ``/exec/status`` on
mount, so a fresh tab shows the truth and then tails live frames.

Cancelling is now something the operator does on purpose, through
``POST /jobs/{id}/cancel``, rather than something that happens because they
closed a tab.

Scope and limits, stated plainly
--------------------------------
* **In-process.** Uvicorn runs single-worker here (``app/run_server.py`` calls
  ``uvicorn.run`` with no ``workers``), so one process owns every run and a
  module-level registry is sufficient. If this ever runs multi-worker, a
  subscriber could land on a worker that does not host the run — it would see
  the durable node state and no live frames. Redis pub/sub is the upgrade path.
* **Not durable across restarts.** The frame buffer is memory. Node state is in
  Postgres and survives; the *event log* of a run does not — replaying a run's
  events across a restart would need Redis, and nothing yet needs it.

  What a restart used to leave behind was worse than a lost event log, though:
  a job sitting at ``running`` with no task behind it, invisible as such, until
  the stale-job reaper noticed — and on this host the reaper's threshold is
  hours, not the 30 minutes the defaults suggest. ``reconcile_on_startup()``
  now settles those rows at boot (§17.1008), so "running" means running.
* **Bounded.** ``MAX_FRAMES`` per run, and finished runs are evicted after
  ``RETAIN_AFTER_END_S`` so a late attacher still sees the terminal frame.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import AsyncGenerator, AsyncIterator, Callable

logger = logging.getLogger("scaffold")

# A long run emits a few frames per node plus progress ticks; 2000 covers a
# 40-node run with room to spare, and caps a runaway producer's memory.
MAX_FRAMES = 2000

# How long a finished run's frames stay attachable. Long enough that a client
# reconnecting through a blip still sees `pipeline_complete`, short enough that
# a busy engine doesn't accumulate dead buffers.
RETAIN_AFTER_END_S = 300.0

# How long a cancel waits for the generator to unwind before returning. The
# operator is holding an HTTP request open on this.
CANCEL_WAIT_S = 10.0


class _Run:
    """One detached run: the task, its frames, and whoever is listening."""

    __slots__ = ("job_id", "task", "frames", "subscribers", "done", "ended_at")

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.task: asyncio.Task | None = None
        self.frames: deque[str] = deque(maxlen=MAX_FRAMES)
        self.subscribers: set[asyncio.Queue[str | None]] = set()
        self.done = asyncio.Event()
        self.ended_at: float | None = None

    def publish(self, frame: str) -> None:
        self.frames.append(frame)
        for q in list(self.subscribers):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # A subscriber too slow to keep up is dropped rather than
                # allowed to stall the RUN. It can reattach; the run is the
                # thing that must not be held back by a browser.
                logger.warning("run_broker_subscriber_dropped job=%s", self.job_id)
                self.subscribers.discard(q)

    def finish(self) -> None:
        self.ended_at = time.monotonic()
        self.done.set()
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)  # sentinel: stream over
            except asyncio.QueueFull:
                pass


_runs: dict[str, _Run] = {}


def _evict_stale() -> None:
    """Drop finished runs past their retention window. Called on each start —
    no timer to own, and the registry only grows when runs are actually made."""
    now = time.monotonic()
    for job_id, run in list(_runs.items()):
        if run.ended_at is not None and now - run.ended_at > RETAIN_AFTER_END_S:
            _runs.pop(job_id, None)


def is_running(job_id: str) -> bool:
    run = _runs.get(job_id)
    return bool(run and not run.done.is_set())


async def _pump(run: _Run, source: AsyncIterator[str]) -> None:
    """Consume the run generator — the ONLY consumer — into the frame buffer."""
    try:
        async for frame in source:
            run.publish(frame)
    except asyncio.CancelledError:
        # An explicit cancel (operator pressed stop, or shutdown). The
        # generator's own finally has already run and settled the job status.
        logger.info("run_broker_cancelled job=%s", run.job_id)
        raise
    except Exception:
        logger.exception("run_broker_pump_failed job=%s", run.job_id)
    finally:
        run.finish()


def start(job_id: str, source_factory: Callable[[], AsyncIterator[str]]) -> _Run:
    """Start a detached run, or return the one already in flight.

    Idempotent by design: two tabs both pressing Run, or a reconnect racing the
    original request, must attach to one run rather than execute the DAG twice.
    """
    _evict_stale()
    existing = _runs.get(job_id)
    if existing is not None and not existing.done.is_set():
        logger.info("run_broker_attach_existing job=%s", job_id)
        return existing

    run = _Run(job_id)
    _runs[job_id] = run
    run.task = asyncio.create_task(_pump(run, source_factory()))
    logger.info("run_broker_started job=%s", job_id)
    return run


async def subscribe(run: _Run) -> AsyncGenerator[str, None]:
    """Yield the run's frames: the backlog first, then live ones.

    Replaying the backlog is what makes a reconnect useful rather than a blank
    event log. It is bounded by ``MAX_FRAMES``; the authoritative node state a
    client needs on attach comes from ``/exec/status``, not from here.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=MAX_FRAMES)
    backlog = list(run.frames)
    run.subscribers.add(queue)
    try:
        for frame in backlog:
            yield frame
        if run.done.is_set() and queue.empty():
            return
        while True:
            frame = await queue.get()
            if frame is None:
                return
            yield frame
    finally:
        # A disconnect lands HERE — dropping this subscriber and nothing else.
        # That is the whole change: the run does not notice.
        run.subscribers.discard(queue)


async def cancel(job_id: str) -> bool:
    """Stop a detached run on purpose. Returns True if one was cancelled.

    With the run no longer tied to the response, closing a tab cannot stop it —
    so stopping needs a verb. This is what ``POST /jobs/{id}/cancel`` calls, and
    what shutdown calls for every run still in flight.
    """
    run = _runs.get(job_id)
    if not run or run.done.is_set() or run.task is None:
        return False
    run.task.cancel()
    try:
        # Bounded: `POST /jobs/{id}/cancel` is a request an operator is waiting
        # on, and the generator's unwind does its own DB work. It cannot hang
        # the response. If the task outlives the wait, the cancellation is
        # already delivered and its cleanup (a detached task in
        # execution_agent, by design) still completes on its own.
        await asyncio.wait_for(asyncio.shield(run.task), timeout=CANCEL_WAIT_S)
    except asyncio.TimeoutError:
        logger.warning("run_broker_cancel_slow job=%s — unwinding in background", job_id)
    except asyncio.CancelledError:
        pass          # the expected outcome: the task honoured the cancel
    except Exception:  # noqa: BLE001 — a failing run is still a stopped run
        pass
    run.finish()       # release subscribers even if the task is still unwinding
    logger.info("run_broker_cancel_requested job=%s", job_id)
    return True


async def reconcile_on_startup() -> None:
    """§17.1008 — settle jobs the previous process left mid-run.

    Detaching a run from its response means a restart is now the only way to
    lose one, and the row it leaves says ``running`` while nothing is running.
    Every consumer reads that as live: the reaper waits out its threshold
    (hours on this host), ``/exec/status`` reports ``detached_running: false``
    beside a running status, and the operator is told the engine restarted
    mid-run but the job never leaves the active list.

    Runs in-process at lifespan startup, BEFORE anything can start a new run,
    so there is no window where a fresh run could be mistaken for a stale row.
    Marks the job ``failed`` (not ``cancelled``: nobody chose this) with an
    error_summary that says what happened, and fails any node still claiming to
    be running. Fail-soft — a reconciliation problem must never stop boot.
    """
    from sqlalchemy import text

    from app.database import async_session

    try:
        async with async_session() as db:
            rows = (await db.execute(text(
                "SELECT id FROM jobs WHERE status IN ('running', 'executing')"
            ))).scalars().all()
            if not rows:
                return
            job_ids = [str(r) for r in rows]
            await db.execute(
                text("""
                    UPDATE jobs
                       SET status = 'failed',
                           error_summary = COALESCE(error_summary, '')
                                         || 'interrupted by an engine restart mid-run',
                           updated_at = NOW()
                     WHERE id = ANY(:ids)
                """),
                {"ids": job_ids},
            )
            await db.execute(
                text("""
                    UPDATE dag_nodes SET status = 'failed', completed_at = NOW(),
                           last_verification_reason = COALESCE(
                               last_verification_reason,
                               'The engine restarted while this step was running, so its result was never recorded. Retry it.')
                     WHERE job_id = ANY(:ids) AND status = 'running'
                """),
                {"ids": job_ids},
            )
            await db.commit()
            logger.warning(
                "run_broker_reconciled_on_startup: %d job(s) left mid-run by a "
                "previous process settled to failed: %s",
                len(job_ids), ", ".join(job_ids[:10]),
            )
    except Exception:  # noqa: BLE001 — never block startup
        logger.exception("run_broker_reconcile_on_startup_failed")


async def shutdown_all() -> None:
    """Cancel every in-flight run at process shutdown, so each generator's
    ``finally`` gets to move its job out of ``running`` instead of leaving it
    for the stale-job reaper."""
    for job_id in list(_runs):
        await cancel(job_id)
    _runs.clear()
