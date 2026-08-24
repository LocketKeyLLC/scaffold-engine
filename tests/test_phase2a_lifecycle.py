"""§17.812 Phase 2A — lifecycle/state-correctness fixes (audit C6, M1).

- C6: init_clients() must run before crash-resume in the lifespan.
- M1: keepalive teardown swallows the children's own cancellation but re-raises
  a cancel delivered to the enclosing task.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

import app.main
from app.modules import execution_agent as ea


# ── 2.6 (C6): init_clients() precedes crash-resume in the lifespan ───────────
@pytest.mark.smoke
def test_init_clients_precedes_crash_resume():
    """A resumed drain reaches the first LLM/embedder call, and the HTTP clients
    have no lazy path — so init_clients() must be called first in the lifespan.

    Compares actual CODE lines (comments stripped) so a docstring/comment that
    mentions either call can't fool the ordering check."""
    src = inspect.getsource(app.main)
    code = [
        ln.strip()
        for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]

    def _first(pred):
        return next((i for i, ln in enumerate(code) if pred(ln)), -1)

    i_init = _first(lambda l: l == "init_clients()")
    i_resume = _first(lambda l: "await resume_orphaned_executions()" in l)
    assert i_init != -1, "init_clients() call not found in app.main"
    assert i_resume != -1, "resume_orphaned_executions() call not found in app.main"
    assert i_init < i_resume, (
        "init_clients() must precede resume_orphaned_executions() (audit C6)"
    )


# ── 2.7 (M1): keepalive teardown cancellation semantics ──────────────────────
@pytest.mark.smoke
async def test_await_keepalives_swallows_child_cancel():
    async def _sleep():
        await asyncio.sleep(100)

    t = asyncio.ensure_future(_sleep())
    await asyncio.sleep(0)  # let it start running
    t.cancel()
    await ea._await_keepalives_cancelled(t)  # must NOT raise
    assert t.cancelled()


@pytest.mark.smoke
async def test_await_keepalives_swallows_child_exception():
    async def _boom():
        raise RuntimeError("child failed while unwinding")

    t = asyncio.ensure_future(_boom())
    await asyncio.sleep(0)
    await ea._await_keepalives_cancelled(t)  # RuntimeError swallowed, no raise


@pytest.mark.smoke
async def test_await_keepalives_reraises_outer_cancel():
    """A CancelledError from a task that is NOT itself cancelled means the
    *current* task was cancelled — it must propagate, not be swallowed."""

    class _FakeTask:
        def cancelled(self):
            return False

        def __await__(self):
            async def _raise():
                raise asyncio.CancelledError()

            return _raise().__await__()

    with pytest.raises(asyncio.CancelledError):
        await ea._await_keepalives_cancelled(_FakeTask())


@pytest.mark.smoke
async def test_await_keepalives_multiple_all_swallowed():
    async def _sleep():
        await asyncio.sleep(100)

    a = asyncio.ensure_future(_sleep())
    b = asyncio.ensure_future(_sleep())
    await asyncio.sleep(0)
    a.cancel()
    b.cancel()
    await ea._await_keepalives_cancelled(a, b)  # both swallowed
    assert a.cancelled() and b.cancelled()
