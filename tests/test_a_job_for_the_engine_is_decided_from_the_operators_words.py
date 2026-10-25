"""§17.1813 — whether a job writes the engine's own code is decided once, at refinement, from the operator's words.

Live (follow-up 5b4880c0, 2026-10-25): the idea said "in the scaffold engine itself (this repository's code)"; the
refiner wrote "inside this repository's scaffold engine" and "in scaffold engine", which `targets_the_engine` does
not read -- so the job's CodeGen steps (T2-T5) ran with no engine block, no name checks and no engine-test sandbox,
and were marked done unchecked. Every reader re-derived the answer from the refiner's paraphrase.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import engine_source as es

IDEA = ("Finish the paper-trading research lab in the scaffold engine itself (this repository's code). It builds on "
        "job f00ea97f, whose finished modules are this job's starting point.")
LIVE_BRIEF = {   # 5b4880c0's refined brief, as the refiner wrote it
    "title": "Finish paper-trading research lab in scaffold engine",
    "description": "Complete the paper-trading research lab inside this repository's scaffold engine, building on the "
                   "finished modules from job f00ea97f (the paper_* modules in app/modules, the audit-trail migration, "
                   "and their tests).",
    "goals": ["Fix paper_order_executor so it uses one consistent field name"],
    "outputs_expected": ["Fixed paper_order_executor with a single consistent ticker/symbol field name"],
}


def test_the_paraphrase_alone_is_not_read_but_the_operators_words_are():
    assert not es.targets_the_engine(LIVE_BRIEF)                        # the gap, as it was
    assert es.idea_targets_the_engine(IDEA)
    assert es.targets_the_engine({**LIVE_BRIEF, "targets_engine": True})
    assert not es.idea_targets_the_engine("Build a CLI that tracks my stocks.")


def _db_with(rows):
    r = MagicMock(); r.all.return_value = rows
    db = AsyncMock(); db.execute = AsyncMock(return_value=r)
    return db


@pytest.mark.asyncio
async def test_a_job_built_on_an_engine_job_writes_the_engine():
    prior = (json.dumps({"description": "Add a lab to the scaffold engine itself"}), "x")
    assert await es.job_targets_the_engine(_db_with([prior]), "j", "Fix the executor.", {"title": "Fix it"})
    assert not await es.job_targets_the_engine(_db_with([({"title": "a CLI"}, "a CLI")]), "j", "Fix it.", {})
    assert not await es.job_targets_the_engine(_db_with([]), "j", "Fix it.", {})


@pytest.mark.asyncio
async def test_the_operators_words_decide_without_reading_the_database():
    db = _db_with([])
    assert await es.job_targets_the_engine(db, "j", IDEA, LIVE_BRIEF)
    db.execute.assert_not_awaited()


@pytest.mark.integration
async def test_the_earlier_job_is_read_from_the_real_database():
    from sqlalchemy import text

    from app.database import async_session
    prior, other, job, lone = (str(uuid.uuid4()) for _ in range(4))
    try:
        async with async_session() as db:
            for jid, brief, idea, meta in (
                    (prior, {"description": "x"}, "Add a lab to this repository's code.", {}),
                    (other, {"description": "a CLI"}, "a CLI", {}),
                    (job, {"title": "Fix it"}, "Fix the executor.", {"builds_on": [other, prior]}),
                    (lone, {"title": "Fix it"}, "Fix the executor.", {"builds_on": [other]})):
                await db.execute(text("INSERT INTO jobs (id, title, status, refined_brief, input_text, metadata) "
                                      "VALUES (:id, 'x', 'completed', CAST(:b AS jsonb), :i, CAST(:m AS jsonb))"),
                                 {"id": jid, "b": json.dumps(brief), "i": idea, "m": json.dumps(meta)})
            await db.commit()
        async with async_session() as db:
            assert await es.job_targets_the_engine(db, job, "Fix the executor.", {"title": "Fix it"})
            assert not await es.job_targets_the_engine(db, lone, "Fix the executor.", {"title": "Fix it"})
    finally:
        async with async_session() as db:
            for jid in (prior, other, job, lone):
                await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


# ── the production path: refinement stamps it, the executor reads it ────────────────────────────────────────

def _refine_db():
    ins = MagicMock(); ins.scalar_one.return_value = "job-1"
    db = AsyncMock(); db.execute = AsyncMock(return_value=ins); db.commit = AsyncMock()

    @asynccontextmanager
    async def nested():
        yield
    db.begin_nested = MagicMock(side_effect=nested)
    return db


async def _refine(idea, brief):
    from app.modules import idea_refinement as ir
    call = MagicMock(); call.arguments = dict(brief)
    resp = MagicMock(success=True, error=None, model="m", total_duration_ms=1, text="", tool_calls=[call])
    with patch.object(ir, "model_router") as mr, patch.object(ir, "transition", new=AsyncMock(return_value=True)) as tr:
        mr.tool_call = AsyncMock(return_value=resp)
        out = await ir.refine_idea(idea, _refine_db())
    return out, json.loads(tr.await_args.kwargs["set_columns"]["refined_brief"])


@pytest.mark.asyncio
async def test_refinement_stamps_the_brief_from_the_operators_words():
    out, saved = await _refine(IDEA, LIVE_BRIEF)
    assert saved["targets_engine"] is True and out["refined_brief"]["targets_engine"] is True


@pytest.mark.asyncio
async def test_refinement_leaves_another_jobs_brief_alone():
    with patch("app.modules.engine_source.job_targets_the_engine", new=AsyncMock(return_value=False)):
        _, saved = await _refine("Build a CLI that tracks my stocks.", {"title": "Stock CLI", "description": "A CLI"})
    assert "targets_engine" not in saved


@pytest.mark.asyncio
async def test_a_failed_check_does_not_fail_the_refinement():
    with patch("app.modules.engine_source.job_targets_the_engine", new=AsyncMock(side_effect=RuntimeError("db"))):
        out, saved = await _refine("Build a CLI.", {"title": "CLI"})
    assert out["status"] == "awaiting_confirmation" and "targets_engine" not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("idea, shown", [(IDEA, True), ("Build a CLI that tracks my stocks.", False)])
async def test_the_executor_reads_the_operators_words_for_a_job_refined_before_the_stamp(idea, shown):
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    with patch("app.sandbox.engine_tests.check", new=AsyncMock(return_value=SimpleNamespace(
                verdict="pass", reason="ok", ran=[]))), \
         patch("app.modules.engine_source.engine_findings", new=AsyncMock(return_value=[])):
        captured, _, _ = await _run(GROUNDED, LIVE_BRIEF, job_extra={"input_text": idea})
    assert ("## The engine's own code (read from the running engine" in captured["prompt"]) is shown, \
        captured.get("raised")
