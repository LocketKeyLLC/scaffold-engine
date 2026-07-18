"""§17.168 — tests for `_run_with_session_lifecycle` cancel-finalize.

The bug: when an SSE client disconnects mid-research, the lifecycle
wrapper's ``finally`` block reached ``_finalize_session(...)``, but the
``await`` was raising ``CancelledError`` (a ``BaseException``) before
the DB UPDATE committed. The prior ``except Exception`` didn't catch
``CancelledError``, so the cancellation propagated out silently and
the session row stayed ``status='running'`` forever — only manual
psql cleanup released the single-running-session guard.

§17.168 wraps the finalize call in ``asyncio.shield`` so the inner
coroutine continues running on the event loop independently of the
caller's cancellation. The DB UPDATE commits even when the surrounding
generator is being closed via aclose() / cancelled.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
class TestRunWithSessionLifecycle:
    async def test_finalizes_completed_session_via_inner_path(self):
        """Happy path: inner coro reaches return normally → outer
        wrapper does NOT call _finalize_session itself (the inner
        flow is expected to have called it with a real terminal
        status like 'completed')."""
        from app.modules.research_state import _run_with_session_lifecycle
        from app.modules import research_state as rs

        async def _inner():
            yield "event1"
            yield "event2"

        finalize_calls = []

        async def _stub_finalize(sid, status, dur, summary=None, error_message=None):
            finalize_calls.append((sid, status, error_message))

        with patch.object(rs, "_ra") as mock_ra:
            mock_ra.return_value._finalize_session = _stub_finalize
            events = []
            async for evt in _run_with_session_lifecycle("sid-1", _inner, 0.0, "topic"):
                events.append(evt)

        assert events == ["event1", "event2"]
        # Wrapper did NOT finalize — that's the inner's responsibility on
        # the happy path. (No finalize calls expected here.)
        assert finalize_calls == []

    async def test_finalizes_failed_on_inner_exception(self):
        """Generic Exception escapes the inner flow → wrapper finalizes
        as 'failed' with a typed error message."""
        from app.modules.research_state import _run_with_session_lifecycle
        from app.modules import research_state as rs

        async def _inner():
            yield "event1"
            raise RuntimeError("boom")

        finalize_calls = []

        async def _stub_finalize(sid, status, dur, summary=None, error_message=None):
            finalize_calls.append((sid, status, error_message))

        with patch.object(rs, "_ra") as mock_ra:
            mock_ra.return_value._finalize_session = _stub_finalize
            events = []
            async for evt in _run_with_session_lifecycle("sid-2", _inner, 0.0, "topic"):
                events.append(evt)

        assert "event1" in events
        # An error SSE event should have been yielded after the inner raise
        assert any("Research failed" in e for e in events)
        assert finalize_calls == [("sid-2", "failed", "RuntimeError: boom")]

    async def test_finalize_completes_despite_outer_cancellation(self):
        """§17.168 load-bearing test. aclose() injects GeneratorExit at
        the current yield point. The wrapper's finally must call
        _finalize_session AND have the DB UPDATE actually complete,
        even though our own task is being cancelled. asyncio.shield is
        what makes this work — without it, the inner await raises
        CancelledError before the UPDATE commits."""
        from app.modules.research_state import _run_with_session_lifecycle
        from app.modules import research_state as rs

        # Inner that yields once then sleeps forever — simulates a
        # research session in mid-flight when the client disconnects.
        async def _inner():
            yield "iteration_started"
            await asyncio.sleep(10000)

        finalize_calls = []

        # Slow finalize: simulates a real DB await. Without shield,
        # the cancellation propagated by aclose would hit this await
        # before it completes.
        async def _slow_finalize(sid, status, dur, summary=None, error_message=None):
            await asyncio.sleep(0.05)
            finalize_calls.append((sid, status, error_message))

        with patch.object(rs, "_ra") as mock_ra:
            mock_ra.return_value._finalize_session = _slow_finalize
            gen = _run_with_session_lifecycle("sid-3", _inner, 0.0, "topic")
            agen = gen.__aiter__()
            # Pull the first event so the inner is suspended at the
            # second yield (inside the sleep).
            first = await agen.__anext__()
            assert first == "iteration_started"
            # Now close the generator — same semantics as Starlette's
            # disconnect-watch wrapper calling aclose on the inner.
            await agen.aclose()

        # The shielded UPDATE must have committed.
        assert finalize_calls == [
            ("sid-3", "cancelled", "client_disconnect"),
        ], (
            "Shield should have let _finalize_session complete despite "
            "the surrounding aclose-driven cancellation. Pre-§17.168 "
            "this assert failed (finalize_calls was empty)."
        )

    async def test_logs_cancel_propagated_warning(self, caplog):
        """When the shielded await sees CancelledError, we log a distinct
        ``finalize_cancel_propagated_but_shielded`` line so the operator
        can correlate stuck-session-recovery events to the disconnect
        cause. The session-state UPDATE still commits via the shielded
        coroutine."""
        from app.modules.research_state import _run_with_session_lifecycle
        from app.modules import research_state as rs

        async def _inner():
            yield "x"
            await asyncio.sleep(10000)

        async def _slow_finalize(sid, status, dur, summary=None, error_message=None):
            await asyncio.sleep(0.05)

        with patch.object(rs, "_ra") as mock_ra, \
             caplog.at_level("WARNING", logger="scaffold.research.state"):
            mock_ra.return_value._finalize_session = _slow_finalize
            gen = _run_with_session_lifecycle("sid-4", _inner, 0.0, "topic")
            agen = gen.__aiter__()
            await agen.__anext__()
            await agen.aclose()

        msgs = [r.message for r in caplog.records]
        assert any("research_cancelled: session=sid-4" in m for m in msgs), (
            "should log research_cancelled when finally fires"
        )
        # The shielded-propagation log is emitted only if CancelledError
        # actually hit the await — depends on test scheduling. We don't
        # require it as a strict assertion; the load-bearing contract is
        # the finalize-completion assert in the prior test.


# ---------------------------------------------------------------------------
# §17.600 — snapshot serializes the real "source" key, not "source_url" (#5)
# ---------------------------------------------------------------------------
def test_build_snapshot_serializes_source_key():
    """Every research producer stores the URL under 'source'; _build_snapshot
    used 'source_url' (always null), so resumed entries silently lost their URL
    and dropped out of the Sources block."""
    from app.modules.research_state import _build_snapshot, ResearchState
    state = ResearchState(topic="t", depth="medium", domain="eng")
    state.all_entries = [
        {"title": "A", "content": "c", "source": "https://example.com/a",
         "confidence_score": 0.9},
    ]
    snap = _build_snapshot(state)
    entry = snap["entries"][0]
    assert entry["source"] == "https://example.com/a"


def test_rehydrate_normalizes_legacy_source_url_key():
    """Legacy snapshots that stored the URL under 'source_url' are normalized
    back to 'source' on resume so consumers reading e['source'] still work."""
    from app.modules import research_agent as ra
    row = {
        "id": "s1", "topic": "t", "depth": "medium", "domain": "eng",
        "state_snapshot": {
            "schema_version": 2,
            "entries": [
                {"title": "A", "content": "c",
                 "source_url": "https://legacy.example/a"},
            ],
        },
    }
    state = ra._rehydrate_state(row)
    assert state.all_entries[0]["source"] == "https://legacy.example/a"
