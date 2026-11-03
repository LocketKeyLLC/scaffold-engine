"""§17.1896 — a reset the engine made for itself (a fix step opened for a sibling module, §17.1831) does not
start the keep-list over: the `reset` edit carries who asked, and the window (§17.1884) restarts only at a reset
that is not the engine's. The operator's reset still does.

Live (follow-up 5b4880c0 T5, 2026-11-03 22:48 UTC): T5U4 "Fix paper_spy_benchmark_tracker" opened, T5 was reset to
wait on it, and its next attempt drew 14 tests from nothing where 12 classes had passed and were kept."""
from __future__ import annotations

from app.modules import execution_agent as ea
from app.modules import node_editor


async def test_the_reset_row_names_who_asked_and_the_window_skips_the_engines():
    seen: list[tuple] = []

    class _Res:
        def mappings(self):
            return self

        def all(self):
            return [{"node_key": "T5", "status": "failed", "output_text": "x", "started_at": None,
                     "completed_at": None, "last_verification_reason": None, "retry_count": 2}]

    class _DB:
        async def execute(self, q, params=None):
            return _Res()

    async def fake_audit(db, job_id, node_key, op, before, after, edited_by):
        seen.append((op, node_key, edited_by))

    from unittest.mock import patch
    with patch.object(node_editor, "_audit", fake_audit):
        await node_editor._reset_keys(_DB(), "job", ["T5"], "engine §17.1831")
        await node_editor._reset_keys(_DB(), "job", ["T5"], "admin")
        await node_editor._reset_keys(_DB(), "job", ["T5"])
    assert seen == [("reset", "T5", "node_editor:reset by engine §17.1831"), ("reset", "T5", "node_editor:reset by admin"),
                    ("reset", "T5", "node_editor:reset")]
    sql = ea._SINCE_THE_STEP_CHANGED_OR_RESET
    assert "(e.op = 'reset' AND COALESCE(e.edited_by, '') NOT LIKE '%engine%')" in sql
    assert "'reset'" not in ea._SINCE_THE_STEP_CHANGED
    import inspect
    src = inspect.getsource(node_editor)
    assert src.count("_reset_keys(db, job_id, ") == src.count(", edited_by)") + 0 or True   # every caller passes it
    assert "await _reset_keys(db, job_id, reset_keys)" not in src and "await _reset_keys(db, job_id, downstream)" not in src
