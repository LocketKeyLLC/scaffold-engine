"""Audit Findings A + B — research_agent extract-batch fall-through diagnostics.

Two related fixes documented in OVERVIEW §17.79's audit-tail items:

  A. The pre-fix `url_mode_extract_failed` (and `pdf_mode_extract_failed`)
     warning logged ``success=True error=None`` in the most common
     fall-through case (model returned 200 OK but no tool_calls — typical
     W.6 brittleness on CPU-only smaller models). The new helper
     ``_classify_extract_no_entries_reason`` returns a tight reason string
     so operators can distinguish "model declined to use the tool" from
     a genuine LLM dispatch failure.

  B. The pre-fix loop logged nothing between the first batch's warning and
     the post-loop `extraction_complete` SSE event. An operator inspecting
     a hung session via `make logs-research` had no way to tell which
     iteration the orchestrator was wedged on. Three new structured INFO
     lines (loop_start / batch_start / batch_done / loop_complete)
     localize a hang to the precise step.
"""
from __future__ import annotations

import logging
import types

import pytest

from app.modules.research_agent import _classify_extract_no_entries_reason


def _resp(*, success=True, error=None):
    r = types.SimpleNamespace()
    r.success = success
    r.error = error
    return r


@pytest.mark.smoke
class TestClassifyExtractNoEntriesReason:
    """Lock the four reason branches so the warn-line wording can't drift back
    to the pre-A success=True/error=None ambiguity."""

    def test_no_response_when_resp_is_none(self):
        assert _classify_extract_no_entries_reason(None, None) == "no_response"

    def test_no_tool_calls_when_success_and_no_args(self):
        # The W.6 brittleness case the fallback exists to handle: 200 OK,
        # non-empty text reply, but model returned no tool_calls.
        assert _classify_extract_no_entries_reason(_resp(success=True), None) == "no_tool_calls"
        assert _classify_extract_no_entries_reason(_resp(success=True), {}) == "no_tool_calls"

    def test_tool_args_missing_entries_when_args_present_but_no_entries(self):
        # Model invoked the tool but produced args without the required key.
        assert _classify_extract_no_entries_reason(
            _resp(success=True), {"summary": "..."},
        ) == "tool_args_missing_entries"

    def test_llm_error_with_short_message_when_success_false(self):
        r = _resp(success=False, error="connection refused")
        assert _classify_extract_no_entries_reason(r, None) == "llm_error:connection refused"

    def test_llm_error_truncates_long_messages(self):
        r = _resp(success=False, error="x" * 200)
        out = _classify_extract_no_entries_reason(r, None)
        assert out.startswith("llm_error:")
        # Capped at 80 chars after the prefix to keep log lines bounded.
        assert len(out) - len("llm_error:") == 80

    def test_llm_error_unknown_when_success_false_no_error_text(self):
        # Defensive: success=False but error is empty string / None.
        r = _resp(success=False, error="")
        assert _classify_extract_no_entries_reason(r, None) == "llm_error:unknown"
        r2 = _resp(success=False, error=None)
        assert _classify_extract_no_entries_reason(r2, None) == "llm_error:unknown"

    def test_resolution_order_is_response_first_then_success_then_args(self):
        # If resp is None, that wins — we don't try to read .success on None.
        assert _classify_extract_no_entries_reason(None, {"entries": []}) == "no_response"
        # If success=False, that wins over the args check.
        assert _classify_extract_no_entries_reason(
            _resp(success=False, error="boom"), {"entries": [{"title": "x"}]},
        ) == "llm_error:boom"


@pytest.mark.smoke
class TestExtractLoopInstrumentation:
    """Audit Finding B — verify the new structured INFO lines fire in order."""

    async def test_url_mode_loop_logs_localize_a_hang(self, caplog):
        """Drive _run_research_url_mode with a stubbed LLM that always returns
        no tool_calls; expect one loop_start, one or more batch_start/batch_done
        per chunk, and one loop_complete. If a future regression makes the
        loop exit silently, this test breaks loudly."""
        from unittest.mock import AsyncMock, patch

        from app.modules.research_agent import _run_research_url_mode, ResearchState

        # Stubbed pieces of the URL-mode pipeline. The LLM call always
        # returns success=True with no tool_calls — the W.6 brittleness
        # case that triggered the original Finding B.
        empty_resp = types.SimpleNamespace(success=True, error=None, tool_calls=[], text="")
        state = ResearchState(topic="https://example.com/x", depth="direct_url")

        with patch("app.modules.research_agent._robots_allowed",
                   new_callable=AsyncMock, return_value=True), \
             patch("app.modules.research_agent._fetch_url_bounded",
                   new_callable=AsyncMock, return_value="<html><body>"
                       + ("Lorem ipsum dolor sit amet. " * 600) + "</body></html>"), \
             patch("app.modules.research_agent.trafilatura.extract",
                   return_value=("Lorem ipsum dolor sit amet. " * 600)), \
             patch("app.modules.research_agent._extract_page_title",
                   new_callable=AsyncMock, return_value="Example"), \
             patch("app.modules.research_agent.model_router.tool_call",
                   new_callable=AsyncMock, return_value=empty_resp), \
             patch("app.modules.research_agent._ingest_and_finalize_direct") as mock_finalize:
            # Stub out the post-extract pipeline so the test stays focused
            # on the loop's logging contract, not the ingest path.
            async def _drained(**_kw):
                if False:  # pragma: no cover — generator with no yields
                    yield ""
            mock_finalize.side_effect = _drained

            with caplog.at_level(logging.INFO, logger="scaffold.research.agent"):
                gen = _run_research_url_mode(
                    url="https://example.com/x",
                    state=state,
                    session_id="sess-test",
                    overrides=None,
                    t0=0.0,
                )
                events = []
                async for ev in gen:
                    events.append(ev)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("url_mode_extract_loop_start" in m for m in msgs), \
            f"missing loop_start; saw: {msgs}"
        assert any("url_mode_extract_batch_start" in m for m in msgs), \
            f"missing batch_start; saw: {msgs}"
        assert any("url_mode_extract_batch_done" in m for m in msgs), \
            f"missing batch_done; saw: {msgs}"
        assert any("url_mode_extract_loop_complete" in m for m in msgs), \
            f"missing loop_complete; saw: {msgs}"
        # The new wording: no more "extract_failed" with success=True.
        assert not any("url_mode_extract_failed" in m for m in msgs), \
            f"old failure wording leaked back: {msgs}"
        assert any("url_mode_extract_no_entries" in m and "no_tool_calls" in m
                   for m in msgs), f"missing no_tool_calls reason in warn: {msgs}"
