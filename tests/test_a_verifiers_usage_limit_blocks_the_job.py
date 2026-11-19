"""§17.1945 — a step failed because the VERIFIER's call reached the model account's usage limit blocks the job, as
a step whose own draw reached it does (§17.1889). Live (5b4880c0 API_REDDIT_AUDIT, 2026-11-05 11:16 UTC): the reason
named the 429 and the session limit, the step was recorded failed, an attempt spent, and the run went on."""
from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from app.modules import execution_agent as ea

_LIVE = ('verifier response error: [role=model_verifier provider=ollama] HTTP 429: {"StatusCode":429,"Status":"429 '
         'Too Many Requests","error":"you (operator) have reached your session usage limit, upgrade for higher '
         'limits: https://ollama.com/upgrade or add usage credits: https://ollama.com/settings/billing"}')


def test_the_verifiers_limit_is_a_quota_and_a_plain_verifier_failure_is_not():
    assert ea._quota_hit({"error": None, "verification_reason": _LIVE})
    assert not ea._quota_hit({"error": None, "verification_reason": "verifier: the output omits the tests"})
    assert not ea._quota_hit({"error": "HTTP 429: Too Many Requests", "verification_reason": ""})   # a retry


@pytest.mark.asyncio
async def test_the_block_carries_the_verifiers_reason():
    seen: dict = {}

    async def fake_transition(db, job_id, *, to, set_columns=None, **kw):
        seen.update({"to": to, "cols": dict(set_columns or {})})
        return "running"

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            return None

    res = {"node_key": "API_REDDIT_AUDIT", "title": "Audit", "error": None, "verification_reason": _LIVE}
    with patch.object(ea, "async_session", lambda: _DB()), \
         patch("app.modules.job_state.transition", side_effect=fake_transition):
        [e async for e in ea._block_for_quota("job-1", "API_REDDIT_AUDIT", res)]
    assert seen["to"] == "blocked" and "session usage limit" in seen["cols"]["error_summary"]


def test_both_loops_hand_the_whole_result_to_the_check():
    for fn in (ea._run_parallel_frontier, ea.execute_all_nodes):
        src = inspect.getsource(fn)
        assert "_quota_hit(res)" in src or "_quota_hit(result)" in src
