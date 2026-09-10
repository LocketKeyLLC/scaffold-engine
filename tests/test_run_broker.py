"""§17.1007 — the detached run broker (app/modules/run_broker.py).

The claim this file exists to hold to account: **closing the tab no longer
stops the run.** Before the broker, ``POST /execute/all`` returned a
StreamingResponse wrapping ``execute_all_nodes`` directly, so Starlette
cancelled the generator on disconnect and its ``finally`` marked the job
cancelled — a 10-25 minute run hostage to a browser tab.

Everything here drives the broker with a stand-in generator rather than the
real executor: the property under test is the broker's lifecycle, and a test
that needed live inference to prove "a disconnect does not cancel" would prove
it about one run on one box, slowly.
"""
from __future__ import annotations

import asyncio

import pytest

from app.modules import run_broker


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test owns the registry — these are module-level singletons."""
    run_broker._runs.clear()
    yield
    run_broker._runs.clear()


def make_source(frames: int = 5, delay: float = 0.01, emitted: list | None = None):
    """A stand-in for execute_all_nodes: yields N frames, slowly."""
    async def source():
        for i in range(frames):
            await asyncio.sleep(delay)
            if emitted is not None:
                emitted.append(i)
            yield f"event: node_done\ndata: {{\"n\": {i}}}\n\n"
    return source


async def drain(agen, limit: int | None = None) -> list[str]:
    out: list[str] = []
    async for frame in agen:
        out.append(frame)
        if limit is not None and len(out) >= limit:
            break
    return out


# ── The headline property ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_survives_its_subscriber_disconnecting():
    """THE point of the change: drop the subscriber after one frame and the
    run still produces every frame it was going to."""
    emitted: list[int] = []
    run = run_broker.start("job-1", make_source(frames=6, emitted=emitted))

    sub = run_broker.subscribe(run)
    got = await drain(sub, limit=1)
    await sub.aclose()          # this is what a closed browser tab does
    assert len(got) == 1

    await asyncio.wait_for(run.done.wait(), timeout=5)
    assert emitted == [0, 1, 2, 3, 4, 5], "the run must not stop when nobody is listening"


@pytest.mark.asyncio
async def test_reattaching_replays_the_backlog_then_streams_live():
    """Reopening the tab shows what was missed, then continues — otherwise a
    reconnect lands on an empty event log and looks broken."""
    run = run_broker.start("job-2", make_source(frames=6, delay=0.02))
    await asyncio.sleep(0.07)  # let a few frames land with nobody attached

    frames = await drain(run_broker.subscribe(run))
    assert len(frames) == 6, f"backlog + live should total every frame, got {len(frames)}"
    assert frames == sorted(frames, key=lambda f: int(f.split('"n": ')[1].split("}")[0]))


@pytest.mark.asyncio
async def test_two_subscribers_both_see_every_frame():
    """Two tabs on the same job watch one run — not two."""
    run = run_broker.start("job-3", make_source(frames=5))
    a, b = await asyncio.gather(
        drain(run_broker.subscribe(run)),
        drain(run_broker.subscribe(run)),
    )
    assert len(a) == 5 and len(b) == 5


# ── Idempotency: one job, one run ────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_is_idempotent_while_a_run_is_live():
    """Two tabs pressing Run, or a reconnect racing the original request, must
    attach to ONE run. Executing the DAG twice would double every node."""
    emitted: list[int] = []
    first = run_broker.start("job-4", make_source(frames=4, emitted=emitted))
    second = run_broker.start("job-4", make_source(frames=4, emitted=emitted))
    assert first is second, "a second start must attach, not launch a second run"

    await asyncio.wait_for(first.done.wait(), timeout=5)
    assert emitted == [0, 1, 2, 3], "the DAG must have been walked exactly once"


@pytest.mark.asyncio
async def test_start_after_completion_launches_a_fresh_run():
    """A finished run is retained so late attachers see the terminal frame, but
    it must not shadow a genuine re-run."""
    first = run_broker.start("job-5", make_source(frames=2))
    await asyncio.wait_for(first.done.wait(), timeout=5)
    second = run_broker.start("job-5", make_source(frames=2))
    assert second is not first


# ── is_running: the signal the client attaches on ────────────────────────

@pytest.mark.asyncio
async def test_is_running_tracks_the_task_not_the_row():
    """`detached_running` in /exec/status comes from here. It has to mean 'a
    task is executing right now', because the client uses it to decide between
    attaching (observation) and starting (an action nobody asked for)."""
    assert run_broker.is_running("job-6") is False
    run = run_broker.start("job-6", make_source(frames=3, delay=0.02))
    assert run_broker.is_running("job-6") is True
    await asyncio.wait_for(run.done.wait(), timeout=5)
    assert run_broker.is_running("job-6") is False


# ── Cancel: stopping is now a deliberate act ─────────────────────────────

@pytest.mark.asyncio
async def test_cancel_actually_stops_the_run():
    """With disconnect demoted to a detach, POST /jobs/{id}/cancel is the only
    thing that stops a run — so it had better stop it."""
    emitted: list[int] = []
    run = run_broker.start("job-7", make_source(frames=50, delay=0.02, emitted=emitted))
    await asyncio.sleep(0.06)
    assert await run_broker.cancel("job-7") is True
    assert run.done.is_set()
    seen = len(emitted)
    await asyncio.sleep(0.1)
    assert len(emitted) == seen, "no frames may be produced after a cancel"
    assert run_broker.is_running("job-7") is False


@pytest.mark.asyncio
async def test_cancel_is_a_no_op_when_nothing_is_running():
    """/jobs/{id}/cancel is called on jobs that never had a detached run; it
    must report False rather than raise, so the DB flip still happens."""
    assert await run_broker.cancel("never-started") is False
    run = run_broker.start("job-8", make_source(frames=1))
    await asyncio.wait_for(run.done.wait(), timeout=5)
    assert await run_broker.cancel("job-8") is False


@pytest.mark.asyncio
async def test_a_subscriber_on_a_cancelled_run_terminates():
    """A cancel must close every open stream, not leave tabs hanging forever."""
    run = run_broker.start("job-9", make_source(frames=50, delay=0.02))
    sub = run_broker.subscribe(run)
    await drain(sub, limit=1)
    await run_broker.cancel("job-9")
    # The remaining frames drain and the generator returns rather than blocking.
    assert await asyncio.wait_for(drain(sub), timeout=2) is not None


# ── Failure and shutdown paths ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_raising_run_still_finishes_its_subscribers():
    """If the executor raises, subscribers must be released — a hung stream is
    worse than an error."""
    async def boom():
        yield "event: node_start\ndata: {}\n\n"
        raise RuntimeError("executor exploded")

    run = run_broker.start("job-10", lambda: boom())
    frames = await asyncio.wait_for(drain(run_broker.subscribe(run)), timeout=2)
    assert len(frames) == 1
    assert run.done.is_set()


@pytest.mark.asyncio
async def test_shutdown_all_cancels_every_live_run():
    """At process shutdown each generator's finally must get to move its job
    out of `running`, rather than leaving it for the stale-job reaper."""
    run_broker.start("job-11", make_source(frames=50, delay=0.02))
    run_broker.start("job-12", make_source(frames=50, delay=0.02))
    await asyncio.sleep(0.03)
    await run_broker.shutdown_all()
    assert run_broker.is_running("job-11") is False
    assert run_broker.is_running("job-12") is False


@pytest.mark.asyncio
async def test_frame_buffer_is_bounded():
    """A long run must not grow memory without limit."""
    original = run_broker.MAX_FRAMES
    try:
        run_broker.MAX_FRAMES = 10
        run = run_broker.start("job-13", make_source(frames=40, delay=0.001))
        await asyncio.wait_for(run.done.wait(), timeout=5)
        assert len(run.frames) <= 10
    finally:
        run_broker.MAX_FRAMES = original
