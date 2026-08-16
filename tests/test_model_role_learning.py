"""§17.803 — role→model learning tests (pure logic + mock-DB).

Covers the decision rule (``select_winner``), the cycle's stage/supersede/skip
behavior, and the apply/dismiss helpers. No live stack — ``run_model_ab_task``
and ``init_clients`` are stubbed; the DB is an ``AsyncMock``. The objective
golden scoring itself is covered by ``test_model_ab.py``.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import model_role_learning as mrl


# ── select_winner (pure) ─────────────────────────────────────────────────────

def _summary(**models) -> dict:
    """models=dict(name={trials,passed,errors,wall})."""
    out = {}
    for name, (trials, passed, errors, wall) in models.items():
        out[name] = {
            "trials": trials, "passed": passed, "errors": errors,
            "wall_s": [wall] if wall is not None else [],
        }
    return out


@pytest.mark.smoke
def test_select_winner_clean_win():
    s = _summary(inc=(5, 4, 0, 10.0), cand=(5, 5, 0, 5.0))
    d = mrl.select_winner(["inc", "cand"], s)
    assert d is not None
    assert d["candidate"] == "cand" and d["incumbent"] == "inc"
    assert d["speedup"] == pytest.approx(2.0)
    assert d["candidate_rate"] == pytest.approx(1.0)


@pytest.mark.smoke
def test_select_winner_faster_but_lower_rate_loses():
    # cand is faster but only 2/5 correct — incumbent's higher rate wins → None.
    s = _summary(inc=(5, 4, 0, 10.0), cand=(5, 2, 0, 5.0))
    assert mrl.select_winner(["inc", "cand"], s) is None


@pytest.mark.smoke
def test_select_winner_equal_rate_but_slower_loses():
    s = _summary(inc=(5, 4, 0, 10.0), cand=(5, 4, 0, 20.0))
    assert mrl.select_winner(["inc", "cand"], s) is None


@pytest.mark.smoke
def test_select_winner_candidate_with_errors_disqualified():
    # cand would win on rate+speed but has a hard error → never proposed.
    s = _summary(inc=(5, 4, 0, 10.0), cand=(5, 5, 2, 5.0))
    assert mrl.select_winner(["inc", "cand"], s) is None


@pytest.mark.smoke
def test_select_winner_unmeasured_incumbent_no_spurious_speedup():
    # incumbent wall == 0 (no trials recorded) must not yield a divide/►win.
    s = _summary(inc=(0, 0, 0, None), cand=(5, 5, 0, 5.0))
    assert mrl.select_winner(["inc", "cand"], s) is None


@pytest.mark.smoke
def test_select_winner_empty_models():
    assert mrl.select_winner([], {}) is None


# ── run_learning_cycle (stage / supersede / skip) ────────────────────────────

def _mock_db_for_stage(new_id: int = 42):
    """execute() → supersede result, then insert result whose scalar()==new_id."""
    supersede_res = MagicMock()
    insert_res = MagicMock()
    insert_res.scalar.return_value = new_id
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[supersede_res, insert_res])
    db.commit = AsyncMock()
    return db


@pytest.mark.smoke
async def test_cycle_skips_roles_without_candidates(monkeypatch):
    monkeypatch.setattr(mrl.settings, "model_role_learning_candidates", {})
    db = AsyncMock()
    out = await mrl.run_learning_cycle(db)
    assert out["staged"] == []
    assert set(out["skipped"]) == set(mrl.ROLE_TASKS)   # every role skipped
    db.execute.assert_not_called()                      # no A/B, no writes


@pytest.mark.smoke
async def test_cycle_stages_proposal_on_clean_win(monkeypatch):
    monkeypatch.setattr(
        mrl.settings, "model_role_learning_candidates",
        {"model_coder": ["cand:cloud"]},
    )
    monkeypatch.setattr(mrl.settings, "model_role_learning_repeat", 1)
    monkeypatch.setattr(mrl.settings, "scheduler_job_timeout", 60)
    monkeypatch.setattr(mrl, "get_model", lambda role: "inc:cloud")

    summary = _summary(**{"inc:cloud": (2, 2, 0, 10.0), "cand:cloud": (2, 2, 0, 4.0)})
    fake_ab = SimpleNamespace(run_model_ab_task=AsyncMock(return_value={"summary": summary}))
    fake_http = SimpleNamespace(init_clients=MagicMock())
    db = _mock_db_for_stage(new_id=7)

    with patch.dict(sys.modules, {"scripts.model_ab": fake_ab,
                                  "app.utils.http_clients": fake_http}):
        out = await mrl.run_learning_cycle(db)

    # incumbent prepended, only the coder role ran.
    fake_ab.run_model_ab_task.assert_awaited_once()
    args, kwargs = fake_ab.run_model_ab_task.await_args
    assert args[0] == "codegen" and args[1] == ["inc:cloud", "cand:cloud"]
    # staged one proposal, superseded-then-inserted (2 executes), committed.
    assert out["staged"] == [{"id": 7, "role": "model_coder", "candidate": "cand:cloud"}]
    assert db.execute.await_count == 2
    supersede_sql = db.execute.await_args_list[0].args[0].text
    assert "status = 'superseded'" in supersede_sql
    insert_sql = db.execute.await_args_list[1].args[0].text
    assert "INSERT INTO model_role_proposals" in insert_sql


@pytest.mark.smoke
async def test_cycle_no_stage_when_incumbent_holds(monkeypatch):
    monkeypatch.setattr(
        mrl.settings, "model_role_learning_candidates",
        {"model_coder": ["cand:cloud"]},
    )
    monkeypatch.setattr(mrl.settings, "model_role_learning_repeat", 1)
    monkeypatch.setattr(mrl.settings, "scheduler_job_timeout", 60)
    monkeypatch.setattr(mrl, "get_model", lambda role: "inc:cloud")

    # candidate slower → incumbent holds → nothing staged.
    summary = _summary(**{"inc:cloud": (2, 2, 0, 4.0), "cand:cloud": (2, 2, 0, 9.0)})
    fake_ab = SimpleNamespace(run_model_ab_task=AsyncMock(return_value={"summary": summary}))
    fake_http = SimpleNamespace(init_clients=MagicMock())
    db = AsyncMock()

    with patch.dict(sys.modules, {"scripts.model_ab": fake_ab,
                                  "app.utils.http_clients": fake_http}):
        out = await mrl.run_learning_cycle(db)

    assert out["staged"] == []
    db.execute.assert_not_called()          # no proposal written


@pytest.mark.smoke
async def test_cycle_failsoft_per_role(monkeypatch):
    monkeypatch.setattr(
        mrl.settings, "model_role_learning_candidates",
        {"model_coder": ["cand:cloud"]},
    )
    monkeypatch.setattr(mrl.settings, "model_role_learning_repeat", 1)
    monkeypatch.setattr(mrl.settings, "scheduler_job_timeout", 60)
    monkeypatch.setattr(mrl, "get_model", lambda role: "inc:cloud")

    fake_ab = SimpleNamespace(run_model_ab_task=AsyncMock(side_effect=RuntimeError("boom")))
    fake_http = SimpleNamespace(init_clients=MagicMock())
    db = AsyncMock()

    with patch.dict(sys.modules, {"scripts.model_ab": fake_ab,
                                  "app.utils.http_clients": fake_http}):
        out = await mrl.run_learning_cycle(db)   # must not raise

    assert out["staged"] == []                    # harness error swallowed


# ── accept / dismiss ─────────────────────────────────────────────────────────

def _row(**cols):
    r = MagicMock()
    r._mapping = dict(cols)
    return r


@pytest.mark.smoke
async def test_accept_applies_override_and_marks(monkeypatch):
    get_res = MagicMock()
    get_res.fetchone.return_value = _row(
        id=1, role="model_coder", candidate_model="cand:cloud", status="open",
    )
    mark_res = MagicMock()
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[get_res, mark_res])
    db.commit = AsyncMock()

    with patch.object(mrl, "set_override", AsyncMock()) as mock_set:
        result = await mrl.accept_proposal(1, db)

    mock_set.assert_awaited_once_with("model_coder", "cand:cloud", db)
    assert result == {"id": 1, "role": "model_coder", "model": "cand:cloud", "applied": True}
    mark_sql = db.execute.await_args_list[1].args[0].text
    assert "status = :status" in mark_sql


@pytest.mark.smoke
async def test_accept_rejects_non_open_no_override(monkeypatch):
    get_res = MagicMock()
    get_res.fetchone.return_value = _row(
        id=1, role="model_coder", candidate_model="cand:cloud", status="accepted",
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=get_res)

    with patch.object(mrl, "set_override", AsyncMock()) as mock_set:
        result = await mrl.accept_proposal(1, db)

    assert result is None
    mock_set.assert_not_awaited()          # never swap a non-open proposal


@pytest.mark.smoke
async def test_accept_missing_returns_none(monkeypatch):
    get_res = MagicMock()
    get_res.fetchone.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=get_res)
    with patch.object(mrl, "set_override", AsyncMock()) as mock_set:
        assert await mrl.accept_proposal(999, db) is None
    mock_set.assert_not_awaited()


@pytest.mark.smoke
async def test_dismiss_open_and_missing():
    hit = MagicMock(); hit.rowcount = 1
    db = AsyncMock(); db.execute = AsyncMock(return_value=hit); db.commit = AsyncMock()
    assert await mrl.dismiss_proposal(1, db) == {"id": 1, "dismissed": True}

    miss = MagicMock(); miss.rowcount = 0
    db2 = AsyncMock(); db2.execute = AsyncMock(return_value=miss); db2.commit = AsyncMock()
    assert await mrl.dismiss_proposal(2, db2) is None
