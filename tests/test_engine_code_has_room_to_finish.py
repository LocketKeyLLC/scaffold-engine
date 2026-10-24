"""§17.1792 — code written for the engine gets an output budget that fits a module and its tests, and an output cut
off at the budget is refused as cut off, not judged as a whole.

Live (job f00ea97f T24, the operator dashboard): every attempt stopped at exactly 8,192 completion tokens
(node_generation_max_tokens); verdicts read "the required tests … entirely absent" and "the module text is truncated
mid-render_dashboard".
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, LIVE_BRIEF, _run


@pytest.mark.asyncio
async def test_engine_code_gets_the_larger_budget_and_other_jobs_do_not():
    with patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])), \
         patch("app.sandbox.engine_tests.check", new=AsyncMock()):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF)
        assert captured["max_tokens"] == max(settings.node_generation_max_tokens, settings.engine_codegen_max_tokens)
        captured, _, _ = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]})
        assert captured["max_tokens"] == settings.node_generation_max_tokens


@pytest.mark.asyncio
async def test_an_output_that_stops_at_the_budget_is_refused_as_cut_off():
    budget = max(settings.node_generation_max_tokens, settings.engine_codegen_max_tokens)
    with patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])), \
         patch("app.sandbox.engine_tests.check", new=AsyncMock()):
        _, reasons, verify = await _run(GROUNDED, LIVE_BRIEF, chat_tokens=budget)
    assert any("§17.1792 the output stopped at the" in r for r in reasons), reasons
    verify.assert_not_awaited()                                   # a half file is not judged
