"""§17.577 — adaptive escalation ladder in retry_failed_node.

Escalation sets the retried node's assigned_model to the rung's model (works for
both serial + parallel re-execution); the exhausted branch optionally hands off
to Assist Mode. All opt-in, fail-soft. AsyncMock-db pattern (no real Postgres).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.modules.assist_agent as aa
from app.config import settings
from app.modules import execution_retry as er


def _row(**fields):
    r = MagicMock()
    for k, v in fields.items():
        setattr(r, k, v)
    return r


def _result_one(row):
    r = MagicMock(); r.fetchone.return_value = row; return r


def _result_all(rows):
    r = MagicMock(); r.fetchall.return_value = rows; return r


def _build_db(target_row, all_rows):
    queue = [_result_one(target_row), _result_all(all_rows)]

    def _side_effect(*a, **k):
        if queue:
            return queue.pop(0)
        u = MagicMock(); u.rowcount = 1
        return u

    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=_side_effect)
    return db


def _reset_update_params(db):
    for call in db.execute.call_args_list:
        if "assigned_model = COALESCE" in str(call.args[0]):
            return call.args[1]
    return None


@pytest.mark.asyncio
async def test_escalation_off_leaves_model(monkeypatch):
    monkeypatch.setattr(settings, "node_escalation_enabled", False)
    db = _build_db(_row(node_key="T1", status="failed", retry_count=0, max_retries=3),
                   [_row(node_key="T1", status="failed", depends_on=[])])
    res = await er.retry_failed_node("j", "T1", db)
    assert res["status"] == "reset"
    assert _reset_update_params(db)["esc_model"] is None   # no escalation


@pytest.mark.asyncio
async def test_escalation_on_sets_rung_model(monkeypatch):
    monkeypatch.setattr(settings, "node_escalation_enabled", True)
    monkeypatch.setattr(settings, "node_escalation_order", ["model_cloud_heavy"])
    monkeypatch.setattr(er, "get_model", lambda role: "STRONG:cloud")
    db = _build_db(_row(node_key="T1", status="failed", retry_count=0, max_retries=3),
                   [_row(node_key="T1", status="failed", depends_on=[])])
    res = await er.retry_failed_node("j", "T1", db)
    assert res["status"] == "reset"
    assert _reset_update_params(db)["esc_model"] == "STRONG:cloud"


@pytest.mark.asyncio
async def test_escalation_clamps_to_last_rung(monkeypatch):
    # retry 5 with a 1-rung order → still resolves the last rung (no IndexError)
    monkeypatch.setattr(settings, "node_escalation_enabled", True)
    monkeypatch.setattr(settings, "node_escalation_order", ["model_cloud_heavy"])
    seen = {}
    monkeypatch.setattr(er, "get_model", lambda role: seen.setdefault("role", role) or "X")
    db = _build_db(_row(node_key="T1", status="failed", retry_count=4, max_retries=9),
                   [_row(node_key="T1", status="failed", depends_on=[])])
    res = await er.retry_failed_node("j", "T1", db)
    assert res["status"] == "reset"
    assert seen["role"] == "model_cloud_heavy"


@pytest.mark.asyncio
async def test_exhausted_escalates_to_assist(monkeypatch):
    monkeypatch.setattr(settings, "node_escalation_enabled", True)
    monkeypatch.setattr(settings, "node_escalation_to_assist", True)
    sas = AsyncMock()
    monkeypatch.setattr(aa, "start_assist_session", sas)
    db = _build_db(_row(node_key="T1", status="failed", retry_count=3, max_retries=3), [])
    res = await er.retry_failed_node("j", "T1", db)
    assert res["status"] == "escalated_to_assist"
    sas.assert_awaited_once()


@pytest.mark.asyncio
async def test_exhausted_assist_off_normal_error(monkeypatch):
    monkeypatch.setattr(settings, "node_escalation_enabled", True)
    monkeypatch.setattr(settings, "node_escalation_to_assist", False)
    db = _build_db(_row(node_key="T1", status="failed", retry_count=3, max_retries=3), [])
    res = await er.retry_failed_node("j", "T1", db)
    assert res["status"] == "error" and "exhausted" in res["message"]


# ---------------------------------------------------------------------------
# §17.854 (audit A6) — Stage 5 reset re-asserts status='failed'; a node
# re-claimed between validate and reset makes the reset a no-op (RETURNING
# None) and retry_failed_node returns an error instead of double-resetting.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_reset_noop_when_node_changed_status(monkeypatch):
    monkeypatch.setattr(settings, "node_escalation_enabled", False)
    # queue: (1) Stage-1 validate sees 'failed', (2) Stage-2 topology,
    # (3) Stage-5 reset UPDATE...RETURNING → fetchone None (lost the race).
    validate = _result_one(_row(node_key="T1", status="failed",
                                retry_count=0, max_retries=3))
    topology = _result_all([_row(node_key="T1", status="failed", depends_on=[])])
    reset_lost = _result_one(None)
    queue = [validate, topology, reset_lost]

    def _side_effect(*a, **k):
        return queue.pop(0) if queue else MagicMock()

    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=_side_effect)

    res = await er.retry_failed_node("j", "T1", db)
    assert res["status"] == "error"
    assert "changed status" in res["message"]
