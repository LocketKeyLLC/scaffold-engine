"""§17.166 — tests for _generate_summary char-budget + timeout fallback.

The bug (Software_design_pattern URL + topic-mode 'function calling'
stalled in summarizing phase for 30 min, blocking subsequent ingests
via the single-running-session guard) was caused by:

1. The summary prompt concatenating up to 60 entries verbatim, which on
   content-heavy pages exceeded Ollama's 4K context. Ollama's behavior
   on context overflow can be to hang indefinitely.
2. No per-call timeout. model_router.generate has a 30-min HTTP timeout
   but no shorter client-side bound on the summary itself.

§17.166 caps prompt body at ~6 KB and wraps the LLM call in
asyncio.wait_for(120s). On timeout: log a warning and return the
same fallback string the resp.success=False path produces.
"""
from tests._research_agent_shared import *  # noqa: F401, F403


class TestBuildSummaryPromptBody:
    def test_empty_entries_returns_empty_string(self):
        from app.modules.research_agent import _build_summary_prompt_body
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        assert _build_summary_prompt_body(state) == ""

    def test_packs_within_budget(self):
        from app.modules.research_agent import (
            _SUMMARY_PROMPT_BUDGET_CHARS, _build_summary_prompt_body,
        )
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        state.all_entries = [
            {"facet": "f", "content": "a" * 100} for _ in range(20)
        ]
        body = _build_summary_prompt_body(state)
        assert len(body) <= _SUMMARY_PROMPT_BUDGET_CHARS
        assert "f" in body
        assert "a" * 100 in body  # full entry preserved when it fits

    def test_truncates_at_budget_not_entry_count(self):
        """Pre-§17.166 capped at entry_count=60. New behavior: stops adding
        entries when the next one would push past the char budget, even if
        we've added far fewer than 60 entries. This is the load-bearing
        change — content-heavy pages no longer blow context."""
        from app.modules.research_agent import (
            _SUMMARY_PROMPT_BUDGET_CHARS, _build_summary_prompt_body,
        )
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        # 5 entries of 2 KB each = 10 KB total; budget is 6 KB so only ~3 fit.
        state.all_entries = [
            {"facet": "f", "content": "X" * 2000} for _ in range(5)
        ]
        body = _build_summary_prompt_body(state)
        assert len(body) <= _SUMMARY_PROMPT_BUDGET_CHARS
        # Should have fit ~3 entries, not all 5
        assert body.count("[f]") < 5

    def test_preserves_first_entries_in_order(self):
        from app.modules.research_agent import _build_summary_prompt_body
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        state.all_entries = [
            {"facet": "first", "content": "A" * 100},
            {"facet": "second", "content": "B" * 100},
        ]
        body = _build_summary_prompt_body(state)
        first_idx = body.index("first")
        second_idx = body.index("second")
        assert first_idx < second_idx

    def test_skips_overflow_entry_does_not_partial_truncate(self):
        """If the next entry would push past budget, skip it entirely —
        don't write a partial line. Keeps the [facet] content format intact
        so the LLM doesn't see a corrupted line."""
        from app.modules.research_agent import (
            _SUMMARY_PROMPT_BUDGET_CHARS, _build_summary_prompt_body,
        )
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        # One small entry that fits + one huge entry that doesn't.
        state.all_entries = [
            {"facet": "small", "content": "x"},
            {"facet": "huge", "content": "Y" * (_SUMMARY_PROMPT_BUDGET_CHARS * 2)},
        ]
        body = _build_summary_prompt_body(state)
        assert "small" in body
        assert "huge" not in body  # skipped entirely, not partially truncated


@pytest.mark.asyncio
class TestGenerateSummaryTimeout:
    async def test_returns_fallback_on_timeout(self):
        """If the LLM call doesn't return within _SUMMARY_PROMPT_TIMEOUT_S,
        _generate_summary logs and returns the same fallback shape as the
        resp.success=False branch.

        Drives the real ``asyncio.wait_for`` with a tiny timeout +
        a hanging stub so the timeout fires + the awaited coroutine
        gets properly cancelled (no RuntimeWarning side effect)."""
        from app.modules.research_agent import _generate_summary
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="my topic", depth="shallow", domain="llm")
        state.all_entries = [{"facet": "f", "content": "content"}]

        async def _hangs(*a, **kw):
            await asyncio.sleep(99999)

        with patch("app.modules.research_agent.model_router.generate",
                   new=_hangs), \
             patch("app.modules.research_agent._SUMMARY_PROMPT_TIMEOUT_S",
                   0.05):
            out = await _generate_summary(state)
        assert "1 entries" in out
        assert "my topic" in out

    async def test_returns_llm_text_on_success(self):
        """Happy path — model returns text, _generate_summary returns the
        stripped string. Pinned so the timeout-fallback contract doesn't
        accidentally swallow successful responses."""
        from app.modules.research_agent import _generate_summary
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="topic", depth="shallow", domain="llm")
        state.all_entries = [{"facet": "f", "content": "c"}]

        from app.providers.base import ModelResponse
        ok = ModelResponse(
            text="  the summary  ", model="m", success=True, provider="ollama",
        )
        # Patch model_router.generate at the call site so the wait_for
        # path actually awaits a coroutine that returns a real response.
        async def _ok_generate(*a, **kw):
            return ok
        with patch("app.modules.research_agent.model_router.generate",
                   new=_ok_generate):
            out = await _generate_summary(state)
        assert out == "the summary"

    async def test_returns_fallback_on_llm_failure(self):
        """If the LLM returns success=False (different from timeout),
        we still get a fallback. This pre-existed but is regression-pinned
        so a future refactor doesn't accidentally swallow the success flag."""
        from app.modules.research_agent import _generate_summary
        from app.modules.research_state import ResearchState
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        state.all_entries = [{"facet": "f", "content": "c"}]

        from app.providers.base import ModelResponse
        fail = ModelResponse(
            model="m", success=False, error="boom", provider="ollama",
        )
        async def _fail_generate(*a, **kw):
            return fail
        with patch("app.modules.research_agent.model_router.generate",
                   new=_fail_generate):
            out = await _generate_summary(state)
        assert "1 entries" in out

    async def test_retries_on_empty_content_then_succeeds(self):
        """§17.559 — the thinking model can return success=True + EMPTY text
        (budget spent on reasoning). One retry lands a real summary instead of
        returning an empty one (the §17.558 analog-filters failure mode)."""
        from app.modules.research_agent import _generate_summary
        from app.modules.research_state import ResearchState
        from app.providers.base import ModelResponse
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        state.all_entries = [{"facet": "f", "content": "c"}]

        calls = {"n": 0}

        async def _empty_then_text(*a, **kw):
            calls["n"] += 1
            text = "   " if calls["n"] == 1 else "real summary text"
            return ModelResponse(text=text, model="m", success=True, provider="ollama")

        with patch("app.modules.research_agent.model_router.generate",
                   new=_empty_then_text):
            out = await _generate_summary(state)
        assert calls["n"] == 2  # retried once
        assert "real summary text" in out

    async def test_falls_back_on_persistent_empty_content(self):
        """§17.559 — empty on both draws → the §17.166 fallback shape, never an
        empty summary."""
        from app.modules.research_agent import _generate_summary
        from app.modules.research_state import ResearchState
        from app.providers.base import ModelResponse
        state = ResearchState(topic="t", depth="shallow", domain="llm")
        state.all_entries = [{"facet": "f", "content": "c"}]

        async def _always_empty(*a, **kw):
            return ModelResponse(text="", model="m", success=True, provider="ollama")

        with patch("app.modules.research_agent.model_router.generate",
                   new=_always_empty):
            out = await _generate_summary(state)
        assert "1 entries" in out  # fallback, not empty


class TestSummaryConstants:
    """Pin the budget + timeout values so regressions surface in code review."""

    def test_budget_is_under_4k_token_context(self):
        """A 4K-token model with ~4 chars/token has roughly 16 KB of
        context. Our prompt body cap should leave 10+ KB of headroom
        for the system prompt + topic header + model overhead."""
        from app.modules.research_agent import _SUMMARY_PROMPT_BUDGET_CHARS
        assert _SUMMARY_PROMPT_BUDGET_CHARS <= 8000

    def test_timeout_well_under_local_timeout(self):
        """The per-call summary timeout must be well under
        ``settings.local_timeout`` (30 min) so a wedged Ollama can't
        hold the session running for the full HTTP ceiling."""
        from app.config import settings
        from app.modules.research_agent import _SUMMARY_PROMPT_TIMEOUT_S
        assert _SUMMARY_PROMPT_TIMEOUT_S < settings.local_timeout / 4


# §17.169 — _bounded_tool_call contract: per-LLM-call timeout with
# synthetic failed-response fallback so existing success=False branches
# at call sites handle the timeout without per-site special-casing.
@pytest.mark.asyncio
class TestBoundedToolCall:
    async def test_returns_response_on_success(self):
        """Happy path — the helper is a transparent passthrough when
        the underlying tool_call completes within the timeout."""
        from app.modules.research_agent import _bounded_tool_call
        from app.providers.base import ModelResponse

        ok = ModelResponse(
            text="extracted", model="m", success=True, provider="ollama",
        )

        async def _fake_tool_call(**kwargs):
            return ok

        with patch("app.modules.research_agent.model_router.tool_call",
                   new=_fake_tool_call):
            out = await _bounded_tool_call(messages=[], tools=[], role="r")

        assert out is ok

    async def test_returns_synthetic_failed_response_on_timeout(self):
        """If the LLM call doesn't return within
        _RESEARCH_LLM_TIMEOUT_S, the helper logs a warning and returns
        a synthetic ModelResponse with success=False so callers'
        fallback branches activate. The synthetic response shape is
        the contract — preserve it."""
        from app.modules.research_agent import _bounded_tool_call

        async def _hangs(**kwargs):
            await asyncio.sleep(99999)

        with patch("app.modules.research_agent.model_router.tool_call",
                   new=_hangs), \
             patch("app.modules.research_agent._RESEARCH_LLM_TIMEOUT_S",
                   0.05):
            out = await _bounded_tool_call(messages=[], tools=[], role="r")

        assert out.success is False
        assert "research_llm_timeout" in (out.error or "")
        assert out.model == "<timeout>"
        assert out.provider == "<timeout>"

    async def test_synthetic_response_triggers_caller_fallback(self):
        """Integration smoke — the synthetic response's success=False +
        empty tool_calls means callers' existing fallback paths fire
        without code changes. Pinned via _extract_entries: a timeout
        on an LLM batch falls back to raw-chunk entries (the existing
        ``if not entries:`` branch at line 610-622)."""
        from app.modules.research_agent import _extract_entries

        # Two synthetic results so the batch loop runs.
        results = [
            {"url": "https://example.com/a", "title": "A",
             "content": "real content from page A " * 5,
             "facet": "f"},
            {"url": "https://example.com/b", "title": "B",
             "content": "real content from page B " * 5,
             "facet": "f"},
        ]

        # _bounded_tool_call returns the synthetic failed response —
        # the call site's read_tool_args returns no entries, fallback
        # branch builds chunk-based entries from the raw content.
        async def _timeout_call(**kwargs):
            from app.providers.base import ModelResponse
            return ModelResponse(
                model="<timeout>", success=False,
                error="research_llm_timeout after 1s",
                provider="<timeout>",
            )

        # Skip the trafilatura fetch step — the test wants to drive the
        # batch loop with the synthetic-failed response.
        async def _fake_fetch(rs):
            return []

        with patch("app.modules.research_agent._bounded_tool_call",
                   new=_timeout_call), \
             patch("app.modules.research_agent._fetch_and_extract",
                   new=_fake_fetch):
            out = await _extract_entries(results, topic="t")

        # Each result becomes one chunk-based entry via the fallback path.
        assert len(out) == 2
        # The fallback entries carry source_type=community + facet
        for e in out:
            assert e["source_type"] == "community"
            assert e["facet"] == "f"


# §17.208 — _bounded_tool_call touches research_sessions.last_activity_at
# on real LLM progress so the §17.85 reaper sees multi-batch topic-mode
# iterations as alive. Gated on resp.success so the §17.169 synthetic-
# failure response does NOT count as progress.
@pytest.mark.asyncio
class TestBoundedToolCallTouch:
    async def test_touches_on_success_when_session_id_provided(self):
        """Happy path — successful tool_call with session_id triggers
        the _touch_last_activity UPDATE."""
        from app.modules.research_agent import _bounded_tool_call
        from app.providers.base import ModelResponse

        async def _ok(**kwargs):
            return ModelResponse(model="m", success=True, provider="ollama")

        touched = []

        async def _fake_touch(sid):
            touched.append(sid)

        with patch("app.modules.research_agent.model_router.tool_call",
                   new=_ok), \
             patch("app.modules.research_agent._touch_last_activity",
                   new=_fake_touch):
            await _bounded_tool_call(
                messages=[], tools=[], role="r",
                session_id="sess-abc",
            )

        assert touched == ["sess-abc"]

    async def test_does_not_touch_when_session_id_omitted(self):
        """Backwards-compat: callers that don't pass session_id
        (e.g. tests, future non-research uses) still work, no touch."""
        from app.modules.research_agent import _bounded_tool_call
        from app.providers.base import ModelResponse

        async def _ok(**kwargs):
            return ModelResponse(model="m", success=True, provider="ollama")

        touched = []

        async def _fake_touch(sid):
            touched.append(sid)

        with patch("app.modules.research_agent.model_router.tool_call",
                   new=_ok), \
             patch("app.modules.research_agent._touch_last_activity",
                   new=_fake_touch):
            await _bounded_tool_call(messages=[], tools=[], role="r")

        assert touched == []

    async def test_does_not_touch_on_timeout(self):
        """§17.167 invariant — a wedged call must NOT touch
        last_activity_at; the reaper relies on staleness to kill it.
        The §17.169 timeout path returns the synthetic-failure response
        but must skip the touch entirely."""
        from app.modules.research_agent import _bounded_tool_call

        async def _hangs(**kwargs):
            await asyncio.sleep(99999)

        touched = []

        async def _fake_touch(sid):
            touched.append(sid)

        with patch("app.modules.research_agent.model_router.tool_call",
                   new=_hangs), \
             patch("app.modules.research_agent._RESEARCH_LLM_TIMEOUT_S",
                   0.05), \
             patch("app.modules.research_agent._touch_last_activity",
                   new=_fake_touch):
            out = await _bounded_tool_call(
                messages=[], tools=[], role="r",
                session_id="sess-xyz",
            )

        assert out.success is False
        assert "research_llm_timeout" in (out.error or "")
        assert touched == []   # no touch on timeout

    async def test_does_not_touch_on_unsuccessful_response(self):
        """A genuine LLM failure (provider returns success=False, not a
        timeout) also must not advance last_activity_at — same reaper
        invariant: only real forward progress counts."""
        from app.modules.research_agent import _bounded_tool_call
        from app.providers.base import ModelResponse

        async def _fail(**kwargs):
            return ModelResponse(
                model="m", success=False, provider="ollama",
                error="upstream said no",
            )

        touched = []

        async def _fake_touch(sid):
            touched.append(sid)

        with patch("app.modules.research_agent.model_router.tool_call",
                   new=_fail), \
             patch("app.modules.research_agent._touch_last_activity",
                   new=_fake_touch):
            out = await _bounded_tool_call(
                messages=[], tools=[], role="r",
                session_id="sess-fail",
            )

        assert out.success is False
        assert touched == []


# ===========================================================================
# §17.662 — research surfaces user-tailored options (only when applicable)
# ===========================================================================


class TestGenerateOptions:
    def _state(self, topic="firewall for a homelab"):
        from app.modules.research_state import ResearchState
        st = ResearchState(topic=topic, depth="shallow", domain="eng")
        st.all_entries = [{"facet": "tools", "content": "x"}]
        return st

    @pytest.mark.asyncio
    async def test_disabled_returns_none_without_call(self):
        from app.modules import research_agent as ra
        with patch.object(ra.settings, "research_options_enabled", False), \
             patch.object(ra, "_bounded_tool_call", new=AsyncMock()) as call:
            out = await ra._generate_options(self._state(), "summary")
        assert out is None
        call.assert_not_called()

    @pytest.mark.asyncio
    async def test_factual_topic_no_options(self):
        # only-when-applicable: has_options=false → no fabricated choices.
        from app.modules import research_agent as ra
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call", new=AsyncMock(return_value=MagicMock())), \
             patch.object(ra, "read_tool_args", return_value={"has_options": False}):
            out = await ra._generate_options(self._state("what port does postgres use"), "5432")
        assert out is None

    @pytest.mark.asyncio
    async def test_decision_topic_surfaces_options(self):
        from app.modules import research_agent as ra
        args = {
            "has_options": True, "decision": "Which firewall?",
            "options": [
                {"label": "OPNsense", "fit": "GUI homelabbers", "tradeoff": "heavier"},
                {"label": "pfSense", "fit": "stability", "tradeoff": "CE lags"},
            ],
            "suggested": "OPNsense", "why": "friendlier UI",
        }
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call", new=AsyncMock(return_value=MagicMock())), \
             patch.object(ra, "read_tool_args", return_value=args):
            out = await ra._generate_options(self._state(), "summary")
        assert out["decision"] == "Which firewall?"
        assert [o["label"] for o in out["options"]] == ["OPNsense", "pfSense"]
        assert out["suggested"] == "OPNsense" and out["why"] == "friendlier UI"

    @pytest.mark.asyncio
    async def test_single_option_is_not_a_branch(self):
        from app.modules import research_agent as ra
        args = {"has_options": True, "decision": "d",
                "options": [{"label": "only", "fit": "f", "tradeoff": "t"}]}
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call", new=AsyncMock(return_value=MagicMock())), \
             patch.object(ra, "read_tool_args", return_value=args):
            out = await ra._generate_options(self._state(), "s")
        assert out is None                     # <2 options → not a real branch

    @pytest.mark.asyncio
    async def test_options_capped_at_max(self):
        from app.modules import research_agent as ra
        many = [{"label": f"L{i}", "fit": "f", "tradeoff": "t"} for i in range(8)]
        args = {"has_options": True, "decision": "d", "options": many, "suggested": "L0"}
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra.settings, "research_options_max", 4), \
             patch.object(ra, "_bounded_tool_call", new=AsyncMock(return_value=MagicMock())), \
             patch.object(ra, "read_tool_args", return_value=args):
            out = await ra._generate_options(self._state(), "s")
        assert len(out["options"]) == 4

    @pytest.mark.asyncio
    async def test_failsoft_on_error_never_raises(self):
        from app.modules import research_agent as ra
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            out = await ra._generate_options(self._state(), "s")
        assert out is None

    def test_render_block_shape(self):
        from app.modules.research_agent import _render_options_block
        blk = _render_options_block({
            "decision": "Which DB?",
            "options": [{"label": "Postgres", "fit": "general", "tradeoff": "scale"},
                        {"label": "Influx", "fit": "metrics", "tradeoff": "niche"}],
            "suggested": "Postgres", "why": "one system to run"})
        assert "🔀 Your options — Which DB?" in blk
        assert "**Postgres**" in blk and "**Influx**" in blk
        assert "I'd lean **Postgres**" in blk and "your call" in blk


class TestSummaryAppendsOptions:
    @pytest.mark.asyncio
    async def test_summary_appends_options_block_when_present(self):
        from app.modules import research_agent as ra
        from app.modules.research_state import ResearchState
        st = ResearchState(topic="firewall", depth="shallow", domain="eng")
        st.all_entries = [{"facet": "f", "content": "some fact"}]
        opts = {"decision": "Which firewall?",
                "options": [{"label": "A", "fit": "x", "tradeoff": "y"},
                            {"label": "B", "fit": "x", "tradeoff": "y"}],
                "suggested": "A", "why": "z"}
        with patch.object(ra.model_router, "generate",
                          new=AsyncMock(return_value=_make_generate_response("A clean prose summary."))), \
             patch.object(ra, "_maybe_cove_revise",
                          new=AsyncMock(side_effect=lambda t, *a, **k: t)), \
             patch.object(ra, "_maybe_score_faithfulness", new=AsyncMock(return_value=None)), \
             patch.object(ra, "_generate_options", new=AsyncMock(return_value=opts)):
            out = await ra._generate_summary(st)
        assert "A clean prose summary." in out
        assert "🔀 Your options" in out and "**A**" in out
        assert st.options == opts               # stamped on state for the payload

    @pytest.mark.asyncio
    async def test_summary_clean_when_no_options(self):
        from app.modules import research_agent as ra
        from app.modules.research_state import ResearchState
        st = ResearchState(topic="what port does postgres use", depth="shallow", domain="eng")
        st.all_entries = [{"facet": "f", "content": "5432"}]
        with patch.object(ra.model_router, "generate",
                          new=AsyncMock(return_value=_make_generate_response("Postgres uses 5432."))), \
             patch.object(ra, "_maybe_cove_revise",
                          new=AsyncMock(side_effect=lambda t, *a, **k: t)), \
             patch.object(ra, "_maybe_score_faithfulness", new=AsyncMock(return_value=None)), \
             patch.object(ra, "_generate_options", new=AsyncMock(return_value=None)):
            out = await ra._generate_summary(st)
        assert "🔀 Your options" not in out     # no fabricated choices
        assert st.options is None


class TestOptionsTailoringV664:
    """§17.664 — context tailoring + suggested-must-be-listed validation."""
    def _state(self, topic="db choice"):
        from app.modules.research_state import ResearchState
        st = ResearchState(topic=topic, depth="shallow", domain="eng")
        st.all_entries = [{"facet": "x", "content": "c"}]
        return st

    _OPTS = [{"label": "A", "fit": "f", "tradeoff": "t"},
             {"label": "B", "fit": "f", "tradeoff": "t"}]

    @pytest.mark.asyncio
    async def test_suggested_not_listed_is_dropped(self):
        from app.modules import research_agent as ra
        args = {"has_options": True, "decision": "d", "options": self._OPTS,
                "suggested": "Z-not-listed", "why": "because"}
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call", new=AsyncMock(return_value=MagicMock())), \
             patch.object(ra, "read_tool_args", return_value=args):
            out = await ra._generate_options(self._state(), "s")
        assert out["suggested"] == "" and out["why"] == ""   # both cleared

    @pytest.mark.asyncio
    async def test_valid_suggested_preserved(self):
        from app.modules import research_agent as ra
        args = {"has_options": True, "decision": "d", "options": self._OPTS,
                "suggested": "B", "why": "because"}
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call", new=AsyncMock(return_value=MagicMock())), \
             patch.object(ra, "read_tool_args", return_value=args):
            out = await ra._generate_options(self._state(), "s")
        assert out["suggested"] == "B" and out["why"] == "because"

    @pytest.mark.asyncio
    async def test_context_threaded_into_prompt(self):
        from app.modules import research_agent as ra
        captured = {}
        async def _fake_call(**kwargs):
            captured["messages"] = kwargs.get("messages")
            return MagicMock()
        args = {"has_options": True, "decision": "d", "options": self._OPTS, "suggested": "A"}
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call", new=_fake_call), \
             patch.object(ra, "read_tool_args", return_value=args):
            await ra._generate_options(self._state(), "summary",
                                       context="run it on a Raspberry Pi with 2GB RAM")
        user_msg = captured["messages"][-1]["content"]
        assert "Raspberry Pi" in user_msg and "goal" in user_msg.lower()

    @pytest.mark.asyncio
    async def test_no_context_omits_goal_line(self):
        from app.modules import research_agent as ra
        captured = {}
        async def _fake_call(**kwargs):
            captured["messages"] = kwargs.get("messages")
            return MagicMock()
        args = {"has_options": True, "decision": "d", "options": self._OPTS, "suggested": "A"}
        with patch.object(ra.settings, "research_options_enabled", True), \
             patch.object(ra, "_bounded_tool_call", new=_fake_call), \
             patch.object(ra, "read_tool_args", return_value=args):
            await ra._generate_options(self._state(), "summary")   # no context
        assert "goal / needs" not in captured["messages"][-1]["content"].lower()


class TestOptionsGoalInherentGuardV667:
    """§17.667 — the options prompt surfaces decisions INHERENT in a build/set-up
    goal (not just explicit comparisons in the research), while excluding trivial
    interchangeable 'choices'. Guards the prompt wording; behaviour is model-driven
    and verified by a live smoke."""

    def test_prompt_covers_goal_inherent_and_material_consequence(self):
        from app.modules.research_agent import OPTIONS_SYSTEM_V1
        low = OPTIONS_SYSTEM_V1.lower()
        # goal-inherent case (build/set up something)
        assert "build" in low and "set up" in low
        # the material-consequence test that keeps it from over-triggering
        assert "materially different" in low or "consequential" in low
        # the interchangeable exclusion (the over-trigger this guards against)
        assert "interchangeable" in low
        # still explicitly false for factual/single-answer
        assert "false" in low and "factual" in low
