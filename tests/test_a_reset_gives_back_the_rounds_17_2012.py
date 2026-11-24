"""§17.2012 — the operator's reset gives a step its extra rounds back, as §17.1752 gives its retries; the engine's own
reset keeps the count.

Live (69fe61c4 NOKEY2, 2026-11-24): both §17.1923 rounds went on attempts the engine's gates refused wrongly
(§17.2008-2011). After those fixes and a reset the step converged 24 -> 20 -> 13 -> 6 failing of 61 and was failed:
`converging_rounds` was still 2 in its tool_config."""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import node_editor

text = node_editor.text


async def _setup(jid: str, cfg: dict) -> None:
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("INSERT INTO jobs (id, title, status, metadata) VALUES (:id, 'rounds', 'blocked', "
                              "CAST(:m AS JSONB))"), {"id": jid, "m": json.dumps({})})
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order, retry_count, max_retries, tool, "
            "tool_config) VALUES (:id, 'NOKEY2', 'Make GET /dashboard answer', 'failed', 0, 3, 3, 'CodeGen', "
            "CAST(:c AS JSONB))"), {"id": jid, "c": json.dumps(cfg)})
        await db.commit()


async def _after(jid: str) -> tuple[dict, list]:
    from app.database import async_session
    async with async_session() as db:
        cfg = (await db.execute(text("SELECT tool_config FROM dag_nodes WHERE job_id = :j"), {"j": jid})).scalar()
        before = [r[0] for r in (await db.execute(text(
            "SELECT before FROM dag_node_edits WHERE job_id = :j AND op = 'reset'"), {"j": jid})).all()]
    return cfg, before


async def _drop(jid: str) -> None:
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


CFG = {"fixes_module": "paper_wire_engine_entry_module", "converging_rounds": 2,
       "converging_from": ["21 failed / 40 passed", "10 failed / 51 passed"]}


@pytest.mark.integration
async def test_the_operators_reset_gives_the_rounds_back_and_keeps_the_rest():
    from app.database import async_session
    jid = str(uuid.uuid4())
    try:
        await _setup(jid, CFG)
        async with async_session() as db:
            assert (await node_editor.reset_node(jid, "NOKEY2", edited_by="admin", db=db))["status"] == "ok"
        cfg, before = await _after(jid)
        assert "converging_rounds" not in cfg
        assert cfg["fixes_module"] == "paper_wire_engine_entry_module" and cfg["converging_from"] == CFG["converging_from"]
        assert any((b or {}).get("converging_rounds") == 2 for b in before), before     # the spent count is on record
    finally:
        await _drop(jid)


@pytest.mark.integration
async def test_the_engines_own_reset_keeps_the_count():
    from app.database import async_session
    jid = str(uuid.uuid4())
    try:
        await _setup(jid, CFG)
        async with async_session() as db:
            await node_editor.reset_node(jid, "NOKEY2", edited_by="engine:measured — x", db=db)
        cfg, _ = await _after(jid)
        assert cfg["converging_rounds"] == 2
    finally:
        await _drop(jid)


@pytest.mark.integration
async def test_a_step_with_no_rounds_or_no_tool_config_resets_as_before():
    from app.database import async_session
    jid = str(uuid.uuid4())
    try:
        await _setup(jid, {"fixes_module": "m"})
        async with async_session() as db:
            assert (await node_editor.reset_node(jid, "NOKEY2", edited_by="admin", db=db))["status"] == "ok"
        cfg, before = await _after(jid)
        assert cfg == {"fixes_module": "m"} and not any("converging_rounds" in (b or {}) for b in before)
    finally:
        await _drop(jid)
