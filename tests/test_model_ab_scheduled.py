"""§17.578 — scheduled re-A/B: run_model_ab_task library API + the scheduler's
recommendation logic.
"""
import pytest

import scripts.model_ab as mab


# ---- library API ----

@pytest.mark.asyncio
async def test_run_model_ab_task_returns_summary(monkeypatch):
    monkeypatch.setattr(mab, "_load_goldens", lambda p: [{"id": "g1"}])

    async def fake_avail(m, url):
        return True

    async def fake_run_one(task, model, golden, *, temperature, max_tokens):
        return {"task": task.name, "model": model, "golden": golden["id"],
                "ok": True, "passed": True, "wall_s": 1.0}

    monkeypatch.setattr(mab, "_is_available", fake_avail)
    monkeypatch.setattr(mab, "_run_one", fake_run_one)

    res = await mab.run_model_ab_task("codegen", ["m1", "m2"], repeat=2)
    assert res["task"] == "codegen"
    assert set(res["summary"]) == {"m1", "m2"}
    assert res["summary"]["m1"]["trials"] == 2
    assert res["summary"]["m1"]["passed"] == 2
    assert len(res["rows"]) == 4                 # 2 models × 1 golden × 2 repeats


@pytest.mark.asyncio
async def test_run_model_ab_task_unknown_task_raises():
    with pytest.raises(ValueError):
        await mab.run_model_ab_task("nope", ["m1"])


# ---- recommendation logic ----

def test_recommend_flags_faster_clean_candidate(caplog):
    from app import scheduler
    summary = {
        "m1": {"trials": 5, "passed": 5, "errors": 0, "wall_s": [4.0] * 5},
        "m2": {"trials": 5, "passed": 5, "errors": 0, "wall_s": [1.5] * 5},
    }
    with caplog.at_level("WARNING"):
        scheduler._log_model_ab_recommendation("codegen", ["m1", "m2"], summary)
    assert any("model_ab_recommend" in r.getMessage() for r in caplog.records)


def test_no_change_when_incumbent_best(caplog):
    from app import scheduler
    summary = {
        "m1": {"trials": 5, "passed": 5, "errors": 0, "wall_s": [1.0] * 5},
        "m2": {"trials": 5, "passed": 3, "errors": 0, "wall_s": [1.0] * 5},
    }
    with caplog.at_level("INFO"):
        scheduler._log_model_ab_recommendation("codegen", ["m1", "m2"], summary)
    assert any("model_ab_no_change" in r.getMessage() for r in caplog.records)


def test_no_recommend_when_candidate_has_errors(caplog):
    from app import scheduler
    # faster but errored → not clean → no recommend
    summary = {
        "m1": {"trials": 5, "passed": 5, "errors": 0, "wall_s": [4.0] * 5},
        "m2": {"trials": 5, "passed": 4, "errors": 1, "wall_s": [1.0] * 4},
    }
    with caplog.at_level("INFO"):
        scheduler._log_model_ab_recommendation("codegen", ["m1", "m2"], summary)
    assert any("model_ab_no_change" in r.getMessage() for r in caplog.records)
    assert not any("model_ab_recommend" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# §17.602 — scheduler-drain cancel must still record the scheduled_jobs result
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_model_ab_drain_cancel_still_writes_result(monkeypatch):
    """A CancelledError (scheduler drain) must record last_status='cancelled'
    via the shielded finally, then re-raise — not drop the result-write."""
    import asyncio
    from unittest.mock import MagicMock
    import app.scheduler as scheduler

    rec = []

    class _FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt, params=None):
            rec.append((str(stmt), params))
            return MagicMock()

        async def commit(self):
            pass

    monkeypatch.setattr(scheduler, "async_session", lambda: _FakeDb())
    monkeypatch.setattr("app.utils.http_clients.init_clients", lambda: None)

    async def _boom(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr("scripts.model_ab.run_model_ab_task", _boom)

    with pytest.raises(asyncio.CancelledError):
        await scheduler._execute_model_ab_job(1, "codegen:mytask", "modelA,modelB")

    writes = [p for (s, p) in rec if "scheduled_jobs" in s]
    assert writes, "result-write was dropped on drain cancel"
    assert writes[0]["st"] == "cancelled"
