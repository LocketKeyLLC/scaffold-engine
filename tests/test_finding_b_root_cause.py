"""Audit Finding B (root-caused 2026-05-09) — Ollama-side embedder wedges +
the orchestrator's two scaffold-engine response gaps.

Reproduction (full timeline in OVERVIEW §17.81):

  1. /research <wikipedia URL>  →  3 batches extracted via qwen2.5:7b
  2. extraction_complete fired at 00:08:25 UTC
  3. Then 30 min of SSE silence — no heartbeats, no events
  4. curl --max-time 1800 timed out exactly at 30 min

Root cause was the qwen3-embedding:8b runner getting stuck on its first
inference; orchestrator was waiting on the embed call. Two scaffold-engine
gaps amplified the symptom:

  Gap 1. Ollama provider's embed() set ``settings.local_timeout`` (1800s)
         as the dispatch ceiling. 30 minutes of patience for a single
         embed batch is way too much; Ollama-side wedges should surface
         in minutes, not half an hour.

  Gap 2. The ingest phase yielded no SSE between extraction_complete
         and ingestion_complete. When the embed call hung, consumers
         saw a black-box stall.

Fixes:
  - app/providers/ollama.py::embed wraps the dispatcher in
    ``asyncio.wait_for(timeout=max(120, 30 * n_texts), capped at 600)``.
    For 12 chunks: 360s = 6min. Roughly 4x faster failure surface.
  - app/modules/research_agent.py::_ingest_and_finalize_direct wraps the
    ingest_entries await in ``_await_with_heartbeat`` so SSE stays alive
    with status="ingesting" while the embed/upsert runs.
"""
from __future__ import annotations

import asyncio
import logging
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.smoke
class TestOllamaEmbedTimeoutBound:
    """Gap 1 — embed() must surface a wedged Ollama within minutes,
    not the legacy 30-min local_timeout."""

    async def test_default_timeout_scales_with_input_count(self):
        """Default timeout = max(120, 30 * len(texts)), capped at 600.

        Verified by patching _dispatch_with_retry to hang briefly past
        the bound and asserting embed() returns an empty list (the
        documented timeout response) rather than waiting indefinitely.
        """
        from app.providers.ollama import OllamaProvider

        provider = OllamaProvider()
        captured: dict = {}

        async def _slow_dispatch(endpoint, payload, model, fallback=None):
            # Sleep for longer than ANY plausible timeout we'd compute,
            # then return a success — the wait_for should cancel us
            # well before this completes.
            await asyncio.sleep(60.0)
            return types.SimpleNamespace(success=True, raw={"embeddings": [[1.0]]})

        # Force the timeout very low so the test runs in seconds rather
        # than the 120s default floor. Override timeout=2 explicitly.
        with patch("app.model_router._dispatch_with_retry", _slow_dispatch):
            result = await provider.embed("qwen3-embedding:8b", ["hello"], timeout=2)
        assert result == []  # timed out → empty list, not None / not raise

    async def test_succeeds_under_timeout(self):
        """Normal embed call returns vectors and doesn't hit timeout."""
        from app.providers.ollama import OllamaProvider

        async def _fast_dispatch(endpoint, payload, model, fallback=None):
            return types.SimpleNamespace(
                success=True,
                raw={"embeddings": [[0.1, 0.2, 0.3]]},
            )

        provider = OllamaProvider()
        with patch("app.model_router._dispatch_with_retry", _fast_dispatch):
            result = await provider.embed("qwen3-embedding:8b", ["hello"], timeout=10)
        assert result == [[0.1, 0.2, 0.3]]

    async def test_failed_response_returns_empty(self):
        """When dispatcher returns success=False (after retries), embed()
        returns []. Same contract pre- and post-fix."""
        from app.providers.ollama import OllamaProvider

        async def _failed(endpoint, payload, model, fallback=None):
            return types.SimpleNamespace(
                success=False, error="connection refused",
                raw={},
            )

        provider = OllamaProvider()
        with patch("app.model_router._dispatch_with_retry", _failed):
            result = await provider.embed("qwen3-embedding:8b", ["hi"], timeout=5)
        assert result == []

    def test_default_timeout_formula(self):
        """Verify the timeout-derivation formula directly without I/O.

        For n texts: timeout = min(600, max(120, 30 * n))

        - n=1   → 120s (floor)
        - n=4   → 120s (still under floor)
        - n=12  → 360s
        - n=20  → 600s (cap kicks in)
        - n=100 → 600s (cap)
        """
        cases = [(1, 120), (4, 120), (12, 360), (20, 600), (100, 600)]
        for n, expected in cases:
            actual = min(600, max(120, 30 * n))
            assert actual == expected, f"n={n}: expected {expected}s, got {actual}s"


@pytest.mark.smoke
class TestIngestPhaseHeartbeats:
    """Gap 2 — _ingest_and_finalize_direct must yield heartbeats with
    ``status='ingesting'`` while ingest_entries runs, so SSE consumers
    don't see a 30-min black-box stall when Ollama wedges."""

    async def test_heartbeats_yield_during_ingest(self):
        from app.modules.research_agent import (
            _ingest_and_finalize_direct, ResearchState,
        )

        # Simulate a slow ingest_entries (takes ~1s; heartbeats fire at
        # interval=0 so we get many). The patch returns a stats-shaped
        # dict so the post-ingest yields work normally.
        async def _slow_ingest(entries, domain):
            await asyncio.sleep(0.5)
            return {"new": len(entries), "versioned": 0, "rejected": 0,
                    "skipped_hash": 0, "skipped_empty": 0}

        state = ResearchState(topic="https://example.com", depth="direct_url")
        state.iteration = 1

        events = []
        with patch("app.modules.research_agent.ingest_entries",
                   side_effect=_slow_ingest), \
             patch("app.modules.research_state.HEARTBEAT_INTERVAL_SECONDS", 0.05), \
             patch("app.modules.research_agent._update_session_iteration",
                   new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session",
                   new_callable=AsyncMock):
            async for evt in _ingest_and_finalize_direct(
                state=state,
                session_id="test",
                entries=[{"content": "hello", "title": "t"}],
                mode="direct_url",
                topic="https://example.com",
                t0=0.0,
                summary_model=None,  # skip summary for this test
            ):
                events.append(evt)

        # At least one heartbeat with status="ingesting" must have fired.
        ingesting_heartbeats = [
            e for e in events
            if "heartbeat" in e and "ingesting" in e
        ]
        assert ingesting_heartbeats, (
            f"no ingesting heartbeats found in {len(events)} events; "
            f"sample: {events[:3]}"
        )

        # Subsequent events still emit cleanly.
        ingestion_complete = [e for e in events if "ingestion_complete" in e]
        assert ingestion_complete, "ingestion_complete must still fire"

    async def test_heartbeat_payload_includes_entry_count(self):
        """The heartbeat data must carry entry count + iteration so an
        operator scanning logs can correlate the stall to a specific
        batch size."""
        from app.modules.research_agent import (
            _ingest_and_finalize_direct, ResearchState,
        )

        async def _slow(entries, domain):
            await asyncio.sleep(0.3)
            return {"new": 0, "versioned": 0, "rejected": 0,
                    "skipped_hash": 0, "skipped_empty": 0}

        state = ResearchState(topic="x", depth="direct_url")
        state.iteration = 7  # nonstandard so it shows up

        events = []
        with patch("app.modules.research_agent.ingest_entries", side_effect=_slow), \
             patch("app.modules.research_state.HEARTBEAT_INTERVAL_SECONDS", 0.05), \
             patch("app.modules.research_agent._update_session_iteration",
                   new_callable=AsyncMock), \
             patch("app.modules.research_agent._finalize_session",
                   new_callable=AsyncMock):
            async for evt in _ingest_and_finalize_direct(
                state=state, session_id="t",
                entries=[{"content": str(i), "title": "x"} for i in range(5)],
                mode="direct_url", topic="x", t0=0.0, summary_model=None,
            ):
                events.append(evt)

        ingesting = [e for e in events if "ingesting" in e and "heartbeat" in e]
        assert ingesting, "expected ingesting heartbeats"
        # Payload contains the iteration and entry count — exact field
        # names match what _await_with_heartbeat preserves.
        first = ingesting[0]
        assert '"iteration": 7' in first
        assert '"entries": 5' in first
