"""Audit Finding C — explicit Ollama extract-model unload before embed.

Discovered while running the N4 repopulation runbook (§17.79) on the
project's reference T480 (16 GB RAM, CPU-only). Twice in succession the
qwen3-embedding:8b runner wedged on its first call after qwen2.5:7b had
just finished extracting — confirmed via direct curl probes returning
zero bytes after 30s and ``ps`` showing the runner at 99% CPU + 6 GB RSS
in state ``SNl`` (multi-threaded sleeping under heavy load). Memory
audit at the moment of wedge: ``free`` reported 663 MB free + 1.8 GB
swap engaged. The two ~6 GB models loaded simultaneously is the squeeze.

Fix: between ``extraction_complete`` and ``_ingest_and_finalize_direct``
in each direct mode (github/url/pdf), POST ``/api/generate`` with
``keep_alive=0`` and the extract model name. Forces Ollama to free
the extractor before the embedder cold-loads.

This is a speculative mitigation for an environmental issue — the
existing Audit Finding B ``embed_timeout`` (120s × n_texts, capped at
600s) still surfaces a wedge if the unload doesn't help.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.smoke
class TestUnloadOllamaModelHelper:
    """The helper is fail-open: any failure logs a warning + returns
    None. The caller never raises."""

    async def test_posts_keep_alive_zero(self):
        from app.modules.research_agent import _unload_ollama_model

        mock_resp = AsyncMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("app.model_router._get_client", return_value=mock_client):
            await _unload_ollama_model("qwen2.5:7b")

        mock_client.post.assert_awaited_once()
        call = mock_client.post.await_args
        url = call.args[0]
        body = call.kwargs["json"]
        assert "/api/generate" in url
        assert body["model"] == "qwen2.5:7b"
        assert body["keep_alive"] == 0
        assert body["stream"] is False
        assert body["prompt"] == ""

    async def test_empty_model_no_op(self):
        """Passing model='' should be a no-op (defensive)."""
        from app.modules.research_agent import _unload_ollama_model

        mock_client = AsyncMock()
        mock_client.post = AsyncMock()
        with patch("app.model_router._get_client", return_value=mock_client):
            await _unload_ollama_model("")
            await _unload_ollama_model(None)  # type: ignore[arg-type]
        mock_client.post.assert_not_called()

    async def test_dispatch_failure_swallowed(self):
        """Failure path: post() raises → helper logs warning + returns None.
        Caller is unaffected."""
        from app.modules.research_agent import _unload_ollama_model

        async def _boom(*args, **kwargs):
            raise RuntimeError("connection refused")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_boom)

        with patch("app.model_router._get_client", return_value=mock_client):
            # Must not raise.
            result = await _unload_ollama_model("qwen2.5:7b")
        assert result is None

    async def test_timeout_swallowed(self):
        """The wait_for(timeout=15) bound: if Ollama itself hangs, the
        helper returns rather than blocking the caller indefinitely.

        Tested by patching asyncio.wait_for to raise TimeoutError
        directly (so we exercise the timeout-handling branch in the
        helper without actually waiting 15 seconds).
        """
        import asyncio

        from app.modules.research_agent import _unload_ollama_model

        async def _wait_for_raises_timeout(coro, timeout):
            # Close the inner coroutine so it doesn't leak; mimic
            # asyncio.wait_for's cancel-on-timeout behavior.
            coro.close()
            raise asyncio.TimeoutError("simulated hang")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock()
        with patch("app.model_router._get_client", return_value=mock_client), \
             patch("app.modules.research_agent.asyncio.wait_for",
                   side_effect=_wait_for_raises_timeout):
            result = await _unload_ollama_model("qwen2.5:7b")
        assert result is None
