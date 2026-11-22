"""§17.1996 — a retry's model escalation keeps the model the engine switched a step to.

Live (dashboard job 69fe61c4, T8 "Add dashboard route", CodeGen): §17.1848 `switch_code_model` moved T8 from
deepseek-v4-pro to deepseek-v4.1-flash on 2026-11-21 22:27:58 UTC (dag_node_edits, "engine §17.1848",
tool_config.model_switched_from = deepseek-v4-pro). The next retry ran `assigned_model = COALESCE(:esc_model, …)` with
the laptop's single rung, `model_cloud_heavy` = deepseek-v4-pro -- the model it was switched away from. Every attempt
since ran on pro (llm_traces 2026-11-22 15:20-15:56). 6 of 6 switched steps stored sit on their `model_switched_from`.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.modules import execution_retry as er

PRO, FLASH = "deepseek-v4-pro:cloud", "deepseek-v4.1-flash:cloud"


@pytest.fixture
def one_heavy_rung(monkeypatch):
    monkeypatch.setattr(settings, "node_escalation_enabled", True)
    monkeypatch.setattr(settings, "node_escalation_order", ["model_cloud_heavy"])
    monkeypatch.setattr(er, "get_model", lambda role: PRO)


def test_a_switched_step_has_no_rung_and_an_unswitched_one_does(one_heavy_rung):
    switched = {"fixes_module": "paper_wire_engine_entry_module", "model_switched_from": PRO,
                "switched_for": ["missing name"]}
    assert er._escalation_model(1, switched) is None
    assert er._escalation_model(3, json.dumps(switched)) is None          # a JSON text column reads the same
    assert er._escalation_model(1, {"fixes_module": "paper_wire_engine_entry_module"}) == PRO
    assert er._escalation_model(2, None) == PRO
    assert er._escalation_model(1, "not json") == PRO                     # an unreadable config is not a switch
    assert er._escalation_model(1, MagicMock()) == PRO                    # nor a row without the column


def _db(tool_config):
    row = MagicMock(node_key="T8", status="failed", retry_count=0, max_retries=3, tool_config=tool_config)
    one = MagicMock(); one.fetchone.return_value = row
    topo = MagicMock(); topo.fetchall.return_value = [MagicMock(node_key="T8", status="failed", depends_on=[])]
    queue = [one, topo]
    db = AsyncMock(); db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=lambda *a, **k: queue.pop(0) if queue else MagicMock())
    return db


def _esc(db):
    return next(c.args[1]["esc_model"] for c in db.execute.call_args_list if "esc_model" in str(c.args[0]))


@pytest.mark.asyncio
async def test_the_retry_writes_no_rung_over_the_switch(one_heavy_rung):
    db = _db({"model_switched_from": PRO})
    assert (await er.retry_failed_node("j", "T8", db))["status"] == "reset"
    assert _esc(db) is None                                    # COALESCE keeps the switched model
    plain = _db({})
    await er.retry_failed_node("j", "T8", plain)
    assert _esc(plain) == PRO                                  # an unswitched step still escalates (§17.577)


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_the_switched_model_survives_the_retry_on_the_real_row(one_heavy_rung):
    from app.database import async_session
    from sqlalchemy import text
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'model switch', 'failed')"),
                             {"id": jid})
            for key, cfg in (("T8", {"model_switched_from": PRO, "switched_for": ["missing name"]}), ("T9", {})):
                await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, tool, status, execution_order, retry_count, "
                    "max_retries, assigned_model, tool_config) VALUES (:j, :k, 'Add dashboard route', 'CodeGen', "
                    "'failed', 0, 0, 3, :m, CAST(:c AS jsonb))"), {"j": jid, "k": key, "m": FLASH, "c": json.dumps(cfg)})
            await db.commit()
        async with async_session() as db:
            assert (await er.retry_failed_node(jid, "T8", db))["status"] == "reset"
            assert (await er.retry_failed_node(jid, "T9", db))["status"] == "reset"
        async with async_session() as db:
            got = dict((await db.execute(text("SELECT node_key, assigned_model FROM dag_nodes WHERE job_id = :j"),
                                         {"j": jid})).fetchall())
        assert got == {"T8": FLASH, "T9": PRO}
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
