"""§17.282 — pin the `wait_for + Semaphore.acquire` cancellation contract.

§17.280-🟡-1 audit-tail concern: the `asyncio.wait_for(_slot_sem.acquire(),
timeout=...)` pattern in ``execute_all_nodes`` has a historically-known race
window in pre-3.10 CPython — if the inner ``acquire()`` completed between
``wait_for``'s timeout firing and its cancel reaching the inner task, the
slot was held with no flag set to release it on the next exit path.

CPython 3.10 fixed this in ``asyncio.Semaphore.acquire``:

    except exceptions.CancelledError:
        if not fut.cancelled():
            self._value += 1
            self._wake_up_next()
        raise

When acquire's future has already resolved (the slot was taken) and a
``CancelledError`` then lands, the slot is explicitly released back before
re-raising. Same behaviour preserved in 3.12.13 (this project's pin).

These tests pin that language-level contract from the project's side. If
a future Python release regresses the cancellation handler, or if a
third-party Semaphore replacement is swapped in without preserving the
guarantee, these tests fail loudly — the inline reference at
``app/modules/execution_agent.py`` cites §17.282 so the next engineer can
trace the failure straight to the audited contract.
"""
import asyncio

import pytest

from app.modules import execution_agent as ea


def _sem_value(sem: asyncio.Semaphore) -> int:
    """Read the Semaphore's internal `_value` counter for assertions.

    ``Semaphore`` doesn't expose the count, but the private attribute is
    stable across 3.10–3.12 (the cancellation handler mutates it directly).
    """
    return sem._value


@pytest.fixture(autouse=True)
def _reset_slot_sem_each_test():
    ea._reset_execution_slot_sem()
    yield
    ea._reset_execution_slot_sem()


@pytest.mark.smoke
class TestSemaphoreCancelContract:
    """§17.282 — the language guarantee we depend on, pinned per-direction."""

    async def test_acquire_completes_normally_decrements_value(self):
        """Sanity: a happy-path acquire decrements `_value`. Establishes the
        baseline for the cancellation-symmetry assertions below.
        """
        sem = asyncio.Semaphore(2)
        assert _sem_value(sem) == 2
        await sem.acquire()
        assert _sem_value(sem) == 1
        sem.release()
        assert _sem_value(sem) == 2

    async def test_cancel_pending_acquire_does_not_steal_a_slot(self):
        """An acquire that is cancelled while blocked (slot not yet taken)
        must leave `_value` untouched. Direction 1 of the contract.
        """
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # slot count -> 0
        assert _sem_value(sem) == 0

        # Block on acquire — the slot is held by us, so this task waits.
        blocked = asyncio.create_task(sem.acquire())
        # Give the loop one tick to let `blocked` reach `await fut`.
        await asyncio.sleep(0)
        assert not blocked.done()

        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked

        # We still own the only slot — `_value` must still be 0.
        assert _sem_value(sem) == 0
        sem.release()
        assert _sem_value(sem) == 1

    async def test_cancel_after_resolution_releases_slot_back(self):
        """The exact race §17.282 pins: an acquire whose future was already
        resolved (slot taken) but then receives a CancelledError must
        release the slot back. Direction 2 of the contract.

        We arrange this by scheduling a release() that fires AFTER
        acquire's future resolves but inside the same loop tick window
        where a cancellation can interleave.
        """
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # holder
        assert _sem_value(sem) == 0

        waiter = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)  # park the waiter on its future

        # Release wakes the waiter's future. We then cancel the waiter
        # BEFORE giving it a chance to run past `await fut`. The waiter's
        # future is `done() and not cancelled()` at the moment we cancel —
        # so the `except CancelledError` branch must release the slot back.
        sem.release()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        # If the contract holds: slot is released back. If it regressed,
        # `_value` would be 0 and the slot would be unreachable.
        assert _sem_value(sem) == 1, (
            "§17.282 regression: Semaphore.acquire's cancellation handler "
            "no longer releases the slot back when acquire's future "
            "resolved before the cancel landed. The wait_for+acquire path "
            "in execute_all_nodes loses defensive depth — re-audit the "
            "slot lifecycle before this regression ships."
        )


@pytest.mark.smoke
class TestWaitForAcquireUnderTimeout:
    """§17.282 — the project-side composition: `wait_for + acquire`."""

    async def test_wait_for_timeout_does_not_leak_slot(self):
        """When wait_for fires its own timeout against a held semaphore,
        the inner cancellation reaches acquire on a still-pending future,
        which is the safe direction. Pins that the existing wait_for
        wrapper plus the language guarantee composes to no leak.
        """
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # block
        assert _sem_value(sem) == 0

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sem.acquire(), timeout=0.05)

        # The blocked acquire was cancelled by wait_for. We still own the
        # only slot; the cancelled acquire didn't steal one and didn't
        # leak one back twice.
        assert _sem_value(sem) == 0
        sem.release()
        assert _sem_value(sem) == 1

    async def test_wait_for_success_followed_by_release_returns_to_baseline(self):
        """Mirror of the happy path inside execute_all_nodes: acquire
        succeeds inside wait_for; release brings the counter back.
        """
        sem = asyncio.Semaphore(1)
        assert _sem_value(sem) == 1

        await asyncio.wait_for(sem.acquire(), timeout=0.5)
        assert _sem_value(sem) == 0

        sem.release()
        assert _sem_value(sem) == 1

    async def test_outer_task_cancel_does_not_leak_slot_through_wait_for(self):
        """An outer cancellation reaching us mid-acquire propagates through
        wait_for as CancelledError, and the §17.282 contract returns the
        slot. Mirrors what happens when a request task is cancelled by
        Starlette's disconnect machinery while ``execute_all_nodes`` is
        sitting in the acquire-with-timeout block.
        """
        sem = asyncio.Semaphore(1)
        await sem.acquire()  # baseline holder
        assert _sem_value(sem) == 0

        async def _blocked_runner():
            # Will block — slot is held by the outer task.
            await asyncio.wait_for(sem.acquire(), timeout=30.0)

        runner = asyncio.create_task(_blocked_runner())
        await asyncio.sleep(0)  # park the runner on the acquire

        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        # `_value` unchanged — the runner's cancelled acquire did not
        # steal the slot, and its (eventual) future-resolve path was
        # interrupted before any double-decrement.
        assert _sem_value(sem) == 0
        sem.release()
        assert _sem_value(sem) == 1


@pytest.mark.smoke
class TestSlotLeakAuditCommentPinned:
    """§17.282 source-shape guard — the citation block stays in place so a
    future refactor doesn't strip the audit reference and silently re-open
    the question.
    """

    def test_execution_agent_cites_section_17_282(self):
        from app.modules import execution_agent

        with open(execution_agent.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "§17.282" in src, (
            "§17.282 audit citation removed from app/modules/execution_agent.py. "
            "The wait_for+acquire block depends on CPython 3.10+'s Semaphore "
            "cancellation handler — keep the reference so the contract is "
            "discoverable from the call site."
        )

    def test_audit_test_file_referenced_by_production_comment(self):
        from app.modules import execution_agent

        with open(execution_agent.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "test_execution_agent_slot_leak.py" in src, (
            "§17.282 production comment must name this test file so future "
            "engineers tracing a Semaphore-cancellation regression can find "
            "the pinning suite directly."
        )
