"""Tests for research_agent — atomic claim + pause/resume, direct-mode finalization, error-message propagation.

Split from the original test_research_agent.py (#9.6).
Shared imports + helpers live in _research_agent_shared.
"""
from tests._research_agent_shared import *  # noqa: F401, F403

class TestAtomicClaimResume:
    """Verify _atomic_claim_for_resume SQL semantics via mocking.

    Real-DB concurrency is exercised manually in the §7 verification checklist
    (two concurrent curl /research/reply calls on the same paused session).
    Here we prove the function issues the correct conditional UPDATE and
    reports success/failure from rowcount.
    """

    @pytest.mark.asyncio
    async def test_returns_true_when_row_claimed(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.modules.research_agent import _atomic_claim_for_resume

        fake_result = MagicMock()
        fake_result.rowcount = 1

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=fake_result)
        mock_db.commit = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        with patch("app.modules.research_agent.async_session", return_value=mock_db):
            won = await _atomic_claim_for_resume("sid-123", "my reply")

        assert won is True
        mock_db.execute.assert_awaited_once()
        mock_db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_row_not_claimed(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.modules.research_agent import _atomic_claim_for_resume

        fake_result = MagicMock()
        fake_result.rowcount = 0

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=fake_result)
        mock_db.commit = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        with patch("app.modules.research_agent.async_session", return_value=mock_db):
            won = await _atomic_claim_for_resume("sid-456", "losing reply")

        assert won is False

    @pytest.mark.asyncio
    async def test_sql_has_paused_status_guard(self):
        """Regression guard: SQL must include WHERE status = 'paused_awaiting_reply'.

        Inspects the SQL source directly rather than the TextClause object,
        making this test independent of pytest-asyncio ordering and any
        stale async_session/sqlalchemy.text patches from earlier tests.
        """
        import inspect
        from app.modules import research_agent
        source = inspect.getsource(research_agent._atomic_claim_for_resume)
        # The function body (excluding docstring) must contain the atomicity markers
        assert "UPDATE research_sessions" in source, "missing UPDATE"
        assert "WHERE id = :sid" in source, "missing id match in WHERE"
        assert "AND status = 'paused_awaiting_reply'" in source, (
            "missing status guard in WHERE — atomicity is compromised"
        )
        assert "rowcount == 1" in source, "missing rowcount check for claim success"

    @pytest.mark.asyncio
    async def test_reply_is_passed_as_parameter(self):
        """Reply text must reach the DB as a bound parameter, not string-interpolated."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.modules.research_agent import _atomic_claim_for_resume

        fake_result = MagicMock()
        fake_result.rowcount = 1

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=fake_result)
        mock_db.commit = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)

        with patch("app.modules.research_agent.async_session", return_value=mock_db):
            await _atomic_claim_for_resume("sid-param", "user reply text")

        params = mock_db.execute.await_args.args[1]
        assert params.get("sid") == "sid-param"
        assert params.get("reply") == "user reply text"


class TestDirectModeFinalization:
    """Exceptions in direct-mode helpers must land in _finalize_session."""

    @pytest.mark.asyncio
    async def test_github_mode_failure_finalizes_with_error(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        finalize_mock = AsyncMock()
        # fetch_repo_content raises a generic RuntimeError (not a GitHub* exception)
        async def _raise(*a, **kw):
            raise RuntimeError("network fail")

        with patch("app.modules.research_agent._guard_concurrent",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session",
                   new_callable=AsyncMock, return_value="sess-gh-fail"), \
             patch("app.modules.research_agent._finalize_session", finalize_mock), \
             patch("app.utils.github_ingest.fetch_repo_content", new=AsyncMock(side_effect=_raise)):

            events = []
            async for sse in run_research("github:foo/bar"):
                events.append(sse)

        # finalize called with status='failed' and a non-None error_message
        assert finalize_mock.await_count >= 1
        call_args = finalize_mock.await_args_list[-1]
        assert call_args.args[1] == "failed"
        err = call_args.kwargs.get("error_message") or (
            call_args.args[4] if len(call_args.args) > 4 else None
        )
        assert err is not None and "network fail" in err

        # error SSE was emitted
        assert any("event: error" in e for e in events)

    @pytest.mark.asyncio
    async def test_url_mode_robots_denied_finalizes(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        finalize_mock = AsyncMock()
        with patch("app.modules.research_agent._guard_concurrent",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session",
                   new_callable=AsyncMock, return_value="sess-url-robots"), \
             patch("app.modules.research_agent._finalize_session", finalize_mock), \
             patch("app.modules.research_agent._robots_allowed",
                   new_callable=AsyncMock, return_value=False):

            events = []
            async for sse in run_research("https://example.com/blocked"):
                events.append(sse)

        assert finalize_mock.await_count >= 1
        assert finalize_mock.await_args_list[-1].args[1] == "failed"
        assert any("event: error" in e for e in events)


class TestRunResearchErrorMessage:
    @pytest.mark.asyncio
    async def test_topic_mode_exception_includes_error_message(self):
        from unittest.mock import AsyncMock, patch
        from app.modules.research_agent import run_research

        finalize_mock = AsyncMock()
        async def _boom(*a, **kw):
            raise ValueError("decompose blew up")

        with patch("app.modules.research_agent._guard_concurrent",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.research_agent._create_session",
                   new_callable=AsyncMock, return_value="sess-topic-fail"), \
             patch("app.modules.research_agent._finalize_session", finalize_mock), \
             patch("app.modules.research_agent._decompose_topic",
                   new_callable=AsyncMock, side_effect=_boom):

            events = []
            async for sse in run_research("some topic", depth="shallow"):
                events.append(sse)

        call = finalize_mock.await_args_list[-1]
        assert call.args[1] == "failed"
        err = call.kwargs.get("error_message") or (
            call.args[4] if len(call.args) > 4 else None
        )
        assert err is not None
        assert "ValueError" in err and "decompose blew up" in err
