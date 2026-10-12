"""§17.1492 — a run that finds every step finished completes the job.

Live (2026-10-12): the operator's approval ran ADD161 — the home lab's last step — through resolve_run; decide then
restarted the run, which found nothing to do and emitted `pipeline_complete`, and `_cleanup_stuck_running_job`'s
"safety default" moved the job running → failed. 131 steps done, 61 skipped, none failed.
"""
import asyncio

from app.modules import execution_agent as ea


class _DB:
    def __init__(self, open_, total):
        self.open_, self.total = open_, total

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        o, t = self.open_, self.total

        class R:
            rowcount = 0

            def mappings(self_inner):
                class M:
                    def first(self2):
                        return {"open": o, "total": t}
                return M()
        return R()

    async def commit(self):
        pass


def _run(monkeypatch, open_, total):
    seen = {}

    async def transition(db, job_id, *, to, expected_from, reason):
        seen["to"] = to
        return None                      # stop before the node cleanup
    monkeypatch.setattr(ea, "transition", transition)
    monkeypatch.setattr(ea, "async_session", lambda: _DB(open_, total))
    asyncio.run(ea._cleanup_stuck_running_job("j", None))
    return seen["to"]


def test_every_step_finished_is_completed(monkeypatch):
    assert _run(monkeypatch, 0, 192) == "completed"


def test_an_open_step_is_still_failed(monkeypatch):
    assert _run(monkeypatch, 1, 192) == "failed"


def test_an_empty_plan_is_not_completed(monkeypatch):
    assert _run(monkeypatch, 0, 0) == "failed"
