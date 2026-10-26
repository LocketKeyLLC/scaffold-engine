"""§17.1793 — a code step for the engine that keeps leaving out part of its own task is split in two.

Live (job f00ea97f T24 "Implement operator dashboard", 23 attempts): six queries, an HTML/CLI view and tests in one
output; every draw dropped `render_dashboard()` or the tests ("entirely absent"). The operator chose the engine split.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import node_editor
from app.modules.execution_retry import retry_failed_node

text = node_editor.text

BRIEF = {"title": "Paper-trading research lab in scaffold engine",
         "description": "Add a paper-trading research lab to the scaffold engine's own codebase."}
TASK = ("Python module `paper_operator_dashboard.py`. Dashboard showing: account value vs SPY over time; open "
        "positions and recent orders. Exports render_dashboard() → HTML/CLI view. In this same step also write "
        "`tests/test_paper_operator_dashboard.py`: pytest tests of what it must do.")
OMITTED = ("The step's headline export render_dashboard() (HTML/CLI view) is entirely absent, the required "
           "tests/test_paper_operator_dashboard.py was not written at all")


async def _job(db, *, reasons: list[str], cfg: dict | None = None) -> str:
    jid = str(uuid.uuid4())
    await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief) VALUES (:id, 'lab', 'running', "
                          "CAST(:b AS jsonb))"), {"id": jid, "b": json.dumps(BRIEF)})
    for i, (k, st, deps) in enumerate([("T22", "done", []), ("T24", "failed", ["T22"]), ("T25", "pending", ["T24"])]):
        nid = (await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, depends_on, prompt_template, tool, "
            "execution_order, retry_count, max_retries, tool_config) VALUES (:j, :k, :t, :s, :d, :p, 'CodeGen', :o, "
            ":rc, 3, CAST(:c AS jsonb)) RETURNING id"),
            {"j": jid, "k": k, "t": "Implement operator dashboard" if k == "T24" else k, "s": st, "d": deps,
             "p": TASK if k == "T24" else f"Python module `{k.lower()}.py`.", "o": i, "rc": 3 if k == "T24" else 0,
             "c": json.dumps(cfg or {}) if k == "T24" else None})).scalar()
        if k == "T24":
            for r in reasons:
                await db.execute(text("INSERT INTO execution_logs (job_id, node_id, log_level, message, details) "
                                      "VALUES (:j, :n, 'warning', :m, CAST(:d AS jsonb))"),
                                 {"j": jid, "n": nid, "m": "Node 'Implement operator dashboard' -> failed",
                                  "d": json.dumps({"reason": r})})
    await db.commit()
    return jid


async def _drop(jid: str) -> None:
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM execution_logs WHERE job_id = :j"), {"j": jid})
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


@pytest.mark.integration
async def test_an_exhausted_step_that_keeps_omitting_is_split_and_starts_again():
    from app.database import async_session
    async with async_session() as db:
        jid = await _job(db, reasons=[OMITTED, OMITTED, "§17.1749 … has no column `ticker`"])
    try:
        async with async_session() as db:
            out = await retry_failed_node(jid, "T24", db)
        assert out["status"] == "reset" and out["split"]["data_step"] == "T24D"
        async with async_session() as db:
            rows = {r["node_key"]: r for r in (await db.execute(text(
                "SELECT node_key, status, depends_on, prompt_template, retry_count, tool_config FROM dag_nodes "
                "WHERE job_id = :j"), {"j": jid})).mappings().all()}
        d, v = rows["T24D"], rows["T24"]
        assert d["status"] == "pending" and list(d["depends_on"]) == ["T22"]
        assert d["prompt_template"].startswith("Python module `paper_operator_dashboard_data.py`: the DATA part")
        assert "tests/test_paper_operator_dashboard_data.py" in d["prompt_template"]
        assert v["status"] == "pending" and v["retry_count"] == 0 and list(v["depends_on"]) == ["T22", "T24D"]
        assert "Import every query and data function `paper_operator_dashboard_data.py` (step T24D) has" in v["prompt_template"]
        assert "written in this module with `async_session` + `text`" in v["prompt_template"]     # §17.1828
        assert "run through the real `paper_operator_dashboard_data` functions against the scratch database" in \
            v["prompt_template"] and "mock `paper_operator_dashboard_data`" not in v["prompt_template"]   # §17.1829
        assert list(rows["T25"]["depends_on"]) == ["T24"]                   # dependents keep the step's key
    finally:
        await _drop(jid)


@pytest.mark.integration
async def test_no_split_without_repeated_omissions_or_when_already_split():
    from app.database import async_session
    async with async_session() as db:
        once = await _job(db, reasons=[OMITTED, "tests failed: assert 0 == 1"])
        done = await _job(db, reasons=[OMITTED, OMITTED], cfg={"split": True})
    try:
        for jid in (once, done):
            async with async_session() as db:
                out = await retry_failed_node(jid, "T24", db)
            assert out["status"] == "error" and "exhausted" in out["message"]
    finally:
        await _drop(once)
        await _drop(done)


# ── §17.1821 — the operator asks for the split ────────────────────────────────────────────────────────────

@pytest.mark.integration
async def test_the_operator_can_ask_for_the_split_whatever_the_refusals_say():
    from fastapi import HTTPException

    from app.database import async_session
    from app.authz import ROLE_ADMIN, Principal
    from app.routers.jobs import split_step_endpoint
    async with async_session() as db:
        jid = await _job(db, reasons=["tests failed: assert 0 == 3", "has no parameter `symbol`"])   # no omission
        done = await _job(db, reasons=[], cfg={"split": True})
    try:
        op = Principal(identity="operator", role=ROLE_ADMIN)
        async with async_session() as db:
            out = await split_step_endpoint(uuid.UUID(jid), "T24", db=db, principal=op)
        assert out["data_step"] == "T24D" and out["data_module"] == "paper_operator_dashboard_data"
        async with async_session() as db:
            rows = {r["node_key"]: r for r in (await db.execute(text(
                "SELECT node_key, status, depends_on FROM dag_nodes WHERE job_id = :j"), {"j": jid})).mappings().all()}
            who = dict((await db.execute(text(
                "SELECT node_key, string_agg(DISTINCT edited_by, ',') FROM dag_node_edits WHERE job_id = :j "
                "AND node_key IN ('T24', 'T24D') AND op IN ('insert', 'edit') GROUP BY node_key"), {"j": jid})).all())
        assert rows["T24D"]["status"] == "pending" and rows["T24"]["status"] == "pending"
        assert list(rows["T24"]["depends_on"]) == ["T22", "T24D"]
        assert who == {"T24": "operator", "T24D": "operator"}                       # the operator asked, both say so
        async with async_session() as db:
            with pytest.raises(HTTPException) as e:
                await split_step_endpoint(uuid.UUID(done), "T24", db=db, principal=op)
        assert e.value.status_code == 409
    finally:
        await _drop(jid)
        await _drop(done)


# ── §17.1828 — the entry half writes what the data half lacks; asking again re-renders a split made before ────────

# T6's entry text as the §17.1821 split wrote it (follow-up 5b4880c0, 2026-10-25).
OLD_ENTRY = ("Python module `paper_daily_signal_proposal_batch.py`: the part of this task that USES the data -- rendering "
             "/ the exported entry points. Import every query and data function from "
             "`paper_daily_signal_proposal_batch_data.py` (step T6D); never re-implement them here. The task: Implement "
             "the batch that runs once a day before US market open and on demand. It writes proposals to the "
             "append-only audit trail with model and data timestamps. In this same step also write "
             "`tests/test_paper_daily_signal_proposal_batch.py`: pytest tests of the rendering / entry points (mock "
             "`paper_daily_signal_proposal_batch_data`'s functions).")


@pytest.mark.integration
async def test_asking_again_re_renders_an_old_split_entry_and_keeps_its_task():
    from app.database import async_session
    from app.authz import ROLE_ADMIN, Principal
    from app.routers.jobs import split_step_endpoint
    async with async_session() as db:
        jid = await _job(db, reasons=[], cfg={"split": True})
        await db.execute(text("UPDATE dag_nodes SET prompt_template = :p WHERE job_id = :j AND node_key = 'T24'"),
                         {"p": OLD_ENTRY, "j": jid})
        await db.commit()
    try:
        op = Principal(identity="operator", role=ROLE_ADMIN)
        async with async_session() as db:
            out = await split_step_endpoint(uuid.UUID(jid), "T24", db=db, principal=op)
        assert out == {"node_key": "T24", "data_step": "T6D", "data_module": "paper_daily_signal_proposal_batch_data",
                       "refreshed": True}
        async with async_session() as db:
            p = (await db.execute(text("SELECT prompt_template FROM dag_nodes WHERE job_id = :j AND node_key = 'T24'"),
                                  {"j": jid})).scalar()
            who = (await db.execute(text("SELECT edited_by FROM dag_node_edits WHERE job_id = :j AND node_key = 'T24' "
                                         "AND op = 'edit'"), {"j": jid})).scalars().all()
        assert "never re-implement them here" not in p
        assert "`paper_daily_signal_proposal_batch_data.py` (step T6D) has; never re-implement those here" in p
        assert "written in this module with `async_session` + `text`" in p
        assert "It writes proposals to the append-only audit trail with model and data timestamps." in p   # task kept
        assert "tests/test_paper_daily_signal_proposal_batch.py" in p and who == ["operator"]
        assert "mock `paper_daily_signal_proposal_batch_data`'s functions" not in p                     # §17.1829
        assert "mock only what reaches outside the engine" in p
        async with async_session() as db:                                       # asked once more: nothing to change
            again = await split_step_endpoint(uuid.UUID(jid), "T24", db=db, principal=op)
        assert again["refreshed"] is False
    finally:
        await _drop(jid)
