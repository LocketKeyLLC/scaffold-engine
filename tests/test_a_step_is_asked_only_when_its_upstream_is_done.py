"""§17.1407 — a step is parked, and run, only while its dependencies are done NOW.

Live, 2026-10-06/07. ADD122 was recorded done; the executor rightly picked ADD123
(`depends_on={ADD122}`) and began drafting it. ADD122's done was false (its GET
answered `{"settings":{}}`), so it was reset — while ADD123's draft was running.
ADD123 was parked anyway: approving it would have run the media-request
capability on top of a settings capability that was no longer there. Readiness is
read fresh (`_peek_next_node`), so this was a race, not a stale cache: nothing
re-checked at the moment of ASKING, or at the moment of the ANSWER.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import decision_pause, execution_agent
from app.modules.execution_agent import unmet_dependencies


class _Rows:
    def __init__(self, keys):
        self._keys = keys

    def __iter__(self):
        return iter([(k,) for k in self._keys])


class _DB:
    def __init__(self, done):
        self.done = set(done)

    async def execute(self, stmt, params=None):
        return _Rows([k for k in (params or {}).get("keys", []) if k in self.done])


@pytest.mark.asyncio
async def test_a_reopened_dependency_is_unmet():
    assert await unmet_dependencies(_DB({"ADD121"}), "j", {"depends_on": ["ADD122"]}) == ["ADD122"]


@pytest.mark.asyncio
async def test_done_and_skipped_dependencies_are_met():
    assert await unmet_dependencies(_DB({"ADD122", "ADD50"}), "j", {"depends_on": ["ADD122", "ADD50"]}) == []


@pytest.mark.asyncio
async def test_no_dependencies_is_met_without_a_query():
    class _Boom:
        async def execute(self, *a, **k):
            raise AssertionError("no query for a node with no dependencies")
    assert await unmet_dependencies(_Boom(), "j", {"depends_on": []}) == []


def test_the_park_re_checks_at_the_moment_of_asking():
    src = inspect.getsource(execution_agent._pause_for_decision)
    i_check = src.index("_unmet = await unmet_dependencies(db, job_id, target)")
    i_park = src.rindex("park_awaiting_decision(db, job_id, target, frame)")
    assert i_check < i_park
    assert "return await _pause_for_decision(job_id, _depth + 1) if _depth < 6 else None" in src


def test_the_answer_re_checks_before_running():
    src = inspect.getsource(decision_pause.resolve_decision)
    assert "unmet_dependencies" in src and '"outcome": "upstream_open"' in src


def test_the_api_says_409_with_the_unmet_step():
    from app.routers import jobs
    src = inspect.getsource(jobs)
    assert 'outcome["outcome"] == "upstream_open"' in src
    assert '"a step this one depends on is no longer done"' in src


def test_the_question_carries_what_it_stands_on():
    """No extra read in resolve_decision: the parked frame records depends_on."""
    src = inspect.getsource(decision_pause.park_awaiting_decision)
    assert '"depends_on": list(node.get("depends_on") or [])' in src
    assert 'waiting.get("depends_on")' in inspect.getsource(decision_pause.resolve_decision)
