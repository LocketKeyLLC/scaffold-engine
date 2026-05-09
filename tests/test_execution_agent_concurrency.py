"""Sprint X.24 — process-wide concurrency cap on execute_all_nodes.

Verifies the global semaphore introduced in app.modules.execution_agent:
* cap is honored (a second run sees ``queued`` while the first holds the slot)
* the slot is released on every exit path (clean, guard rejection, timeout)
* the queue timeout produces a 503-shaped error SSE
"""
import asyncio

from tests._execution_agent_shared import *  # noqa: F401, F403

from app.config import settings
from app.modules import execution_agent as ea


@pytest.fixture(autouse=True)
def _reset_slot_sem():
    """Drop the cached semaphore so each test re-reads settings."""
    ea._reset_execution_slot_sem()
    yield
    ea._reset_execution_slot_sem()


@pytest.fixture
def _cap_one(monkeypatch):
    """Set the global cap to 1 for the duration of a test."""
    monkeypatch.setattr(settings, "execution_global_concurrency", 1)
    monkeypatch.setattr(settings, "execution_queue_timeout_seconds", 1800)
    ea._reset_execution_slot_sem()


def _make_happy_path_session(*, dag_node_count: int = 1):
    """Mock async_session that satisfies one happy-path execution."""
    guard_result = MagicMock(); guard_result.rowcount = 1
    dag_check = MagicMock(); dag_check.scalar.return_value = dag_node_count
    cleanup_status = MagicMock(); cleanup_status.scalar.return_value = "completed"
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        guard_result,    # Session 1 atomic guard
        dag_check,       # Session 3 DAG count
        cleanup_status,  # finally cleanup status check
    ] + [MagicMock()] * 8)
    db.commit = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _patches_for_one_node(slow_event: asyncio.Event | None = None):
    """Patch _get_job / _peek_next_node / execute_next_node for one node + complete.

    If ``slow_event`` is given, ``execute_next_node`` waits on it before
    returning the first node's result, so the caller can hold the slot.
    """
    mock_get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})
    mock_peek = AsyncMock(side_effect=[
        {"node_key": "T1", "title": "T", "tool": "LLM"},
        None,
    ])

    if slow_event is None:
        mock_exec = AsyncMock(side_effect=[
            {"status": "done", "node_key": "T1", "title": "T",
             "output": "out", "verified": True, "confidence": 0.9,
             "model_used": "qwen2.5:7b"},
            {"status": "complete"},
        ])
    else:
        results = [
            {"status": "done", "node_key": "T1", "title": "T",
             "output": "out", "verified": True, "confidence": 0.9,
             "model_used": "qwen2.5:7b"},
            {"status": "complete"},
        ]
        idx = {"i": 0}

        async def _exec(*args, **kwargs):
            i = idx["i"]
            idx["i"] += 1
            if i == 0:
                await slow_event.wait()
            return results[i]

        mock_exec = _exec

    return mock_get_job, mock_peek, mock_exec


@pytest.mark.smoke
class TestExecutionConcurrencyCap:
    """X.24: a process-wide cap on parallel /execute/all runs."""

    async def test_no_queued_event_when_slot_free(self, _cap_one):
        """Single run with cap=1 → does not emit ``queued`` (slot was free)."""
        session = _make_happy_path_session()
        get_job, peek, exec_next = _patches_for_one_node()

        with patch("app.modules.execution_agent.async_session", session), \
             patch("app.modules.execution_agent._get_job", get_job), \
             patch("app.modules.execution_agent._peek_next_node", peek), \
             patch("app.modules.execution_agent.execute_next_node", exec_next):
            events = await _collect_sse(ea.execute_all_nodes("job-1"))

        assert "queued" not in [e[0] for e in events]

    async def test_cap_one_second_run_sees_queued(self, _cap_one):
        """Slot pre-held → run emits ``queued`` first, then proceeds once
        the slot is released. (Two-run concurrency would require interleaved
        ``mock.patch`` blocks which race on module attributes; pre-acquiring
        the semaphore directly is equivalent and deterministic.)"""
        sem = ea._get_execution_slot_sem()
        await sem.acquire()

        sess = _make_happy_path_session()
        get_job, peek, exec_next = _patches_for_one_node()
        events: list = []

        async def _drain():
            with patch("app.modules.execution_agent.async_session", sess), \
                 patch("app.modules.execution_agent._get_job", get_job), \
                 patch("app.modules.execution_agent._peek_next_node", peek), \
                 patch("app.modules.execution_agent.execute_next_node", exec_next):
                async for chunk in ea.execute_all_nodes("job-X"):
                    events.append(chunk)

        drain_task = asyncio.create_task(_drain())
        try:
            for _ in range(60):
                await asyncio.sleep(0.05)
                if any("event: queued" in c for c in events):
                    break
            assert any("event: queued" in c for c in events), (
                "queued event not emitted while slot was held"
            )
            sem.release()
            await asyncio.wait_for(drain_task, timeout=5)
        finally:
            if not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass

        names = []
        for chunk in events:
            for block in chunk.strip().split("\n\n"):
                for line in block.split("\n"):
                    if line.startswith("event: "):
                        names.append(line[7:])
        assert names[0] == "queued", names
        assert "pipeline_complete" in names, names

    async def test_slot_released_on_guard_rejection(self, _cap_one):
        """Session 1 guard rejection → slot is released (next run is not queued)."""
        # Run 1: guard rejects (rowcount=0).
        guard_fail = MagicMock(); guard_fail.rowcount = 0
        db_a = AsyncMock()
        db_a.execute = AsyncMock(return_value=guard_fail)
        db_a.commit = AsyncMock()
        ctx_a = AsyncMock()
        ctx_a.__aenter__ = AsyncMock(return_value=db_a)
        ctx_a.__aexit__ = AsyncMock(return_value=False)
        sess_a = MagicMock(return_value=ctx_a)
        get_job_a = AsyncMock(return_value={"status": "running", "id": "job-A"})

        with patch("app.modules.execution_agent.async_session", sess_a), \
             patch("app.modules.execution_agent._get_job", get_job_a):
            await _collect_sse(ea.execute_all_nodes("job-A"))

        # Run 2 (after Run 1 returned): happy path. Should not see ``queued``.
        sess_b = _make_happy_path_session()
        get_job_b, peek_b, exec_b = _patches_for_one_node()

        with patch("app.modules.execution_agent.async_session", sess_b), \
             patch("app.modules.execution_agent._get_job", get_job_b), \
             patch("app.modules.execution_agent._peek_next_node", peek_b), \
             patch("app.modules.execution_agent.execute_next_node", exec_b):
            events_b = await _collect_sse(ea.execute_all_nodes("job-B"))

        assert "queued" not in [e[0] for e in events_b]

    async def test_queue_timeout_emits_503_error(self, monkeypatch):
        """When the cap is full and timeout expires → ``error`` SSE with http_status=503."""
        monkeypatch.setattr(settings, "execution_global_concurrency", 1)
        monkeypatch.setattr(settings, "execution_queue_timeout_seconds", 1)
        ea._reset_execution_slot_sem()

        # Pre-acquire the slot directly so the next run has to wait.
        sem = ea._get_execution_slot_sem()
        await sem.acquire()
        try:
            # Patch wait_for to raise TimeoutError immediately (so the test
            # doesn't have to actually sleep ``execution_queue_timeout_seconds``).
            real_wait_for = asyncio.wait_for

            async def _instant_timeout(coro, timeout):
                # Cancel the inner acquire coroutine so we don't leak it.
                fut = asyncio.ensure_future(coro)
                fut.cancel()
                try:
                    await fut
                except (asyncio.CancelledError, BaseException):
                    pass
                raise asyncio.TimeoutError

            monkeypatch.setattr(
                "app.modules.execution_agent.asyncio.wait_for", _instant_timeout
            )

            events = await _collect_sse(ea.execute_all_nodes("job-X"))
        finally:
            sem.release()

        # First event is ``queued``, then ``error`` with http_status=503.
        names = [e[0] for e in events]
        assert names == ["queued", "error"], names
        assert events[1][1]["http_status"] == 503
        assert "queue timeout" in events[1][1]["message"].lower()

    async def test_cancel_during_session3_releases_slot_and_cleans_job(
        self, _cap_one, monkeypatch,
    ):
        """Regression for the live-verification finding: cancellation while
        Session 3 (auto-DAG generation) was awaiting an LLM call leaked the
        slot and left the job stuck at ``running`` because the cleanup
        ``await`` inside ``finally`` was interrupted by re-entrant
        cancellation. With the spawn-detached cleanup pattern, both the
        slot and the DB row recover."""
        guard_result = MagicMock(); guard_result.rowcount = 1
        # DAG count = 0 → execute_all_nodes calls dag_generator.generate_dag.
        dag_check = MagicMock(); dag_check.scalar.return_value = 0
        # Background cleanup task: SELECT status, then UPDATE jobs, UPDATE dag_nodes.
        cleanup_status = MagicMock(); cleanup_status.scalar.return_value = "running"
        cleanup_update_jobs = MagicMock()
        cleanup_update_nodes = MagicMock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            guard_result,        # Session 1 atomic guard UPDATE
            dag_check,           # Session 3 SELECT COUNT(*) FROM dag_nodes
            cleanup_status,      # cleanup task: SELECT status
            cleanup_update_jobs, # cleanup task: UPDATE jobs SET status
            cleanup_update_nodes,# cleanup task: UPDATE dag_nodes
        ])
        db.commit = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=db)
        ctx.__aexit__ = AsyncMock(return_value=False)
        sess = MagicMock(return_value=ctx)

        get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})

        # Patch generate_dag to raise CancelledError — simulates a real
        # disconnect during the LLM call inside Session 3.
        async def _cancelling_gen_dag(*args, **kwargs):
            raise asyncio.CancelledError()

        # generate_dag is imported lazily inside execute_all_nodes; patch the
        # source module so the import binding picks up the mock.
        from app.modules import dag_generator
        monkeypatch.setattr(dag_generator, "generate_dag", _cancelling_gen_dag)

        async def _run():
            reraised = False
            with patch("app.modules.execution_agent.async_session", sess), \
                 patch("app.modules.execution_agent._get_job", get_job):
                try:
                    async for _ in ea.execute_all_nodes("job-1"):
                        pass
                except asyncio.CancelledError:
                    reraised = True
                # Cleanup runs detached — drain inside the patch.
                await ea.drain_cleanup_tasks()
            return reraised

        reraised = await _run()
        assert reraised, "CancelledError must propagate out (framework needs it)"

        # Slot must be released for subsequent runs.
        sem = ea._get_execution_slot_sem()
        assert not sem.locked(), "Slot leaked after Session 3 cancellation"

        # DB cleanup must have flipped the job to 'cancelled'.
        update_calls = [
            c for c in db.execute.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict)
               and c.args[1].get("s") == "cancelled"
        ]
        assert update_calls, "Cleanup UPDATE with status='cancelled' did not fire"

    async def test_cap_two_allows_one_concurrent_acquire(self, monkeypatch):
        """cap=2 → with one slot already taken, a run still acquires the
        second slot without queueing."""
        monkeypatch.setattr(settings, "execution_global_concurrency", 2)
        monkeypatch.setattr(settings, "execution_queue_timeout_seconds", 1800)
        ea._reset_execution_slot_sem()

        sem = ea._get_execution_slot_sem()
        await sem.acquire()  # 1 of 2 taken; the next run should NOT queue.
        try:
            sess = _make_happy_path_session()
            get_job, peek, exec_next = _patches_for_one_node()

            with patch("app.modules.execution_agent.async_session", sess), \
                 patch("app.modules.execution_agent._get_job", get_job), \
                 patch("app.modules.execution_agent._peek_next_node", peek), \
                 patch("app.modules.execution_agent.execute_next_node", exec_next):
                events = await _collect_sse(ea.execute_all_nodes("job-Y"))
        finally:
            sem.release()

        assert "queued" not in [e[0] for e in events]
        assert "pipeline_complete" in [e[0] for e in events]
