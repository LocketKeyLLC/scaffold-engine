"""§17.1192 — a recipe walkthrough must not ask for what the engine holds.

The write-channel walkthrough opened with nine open questions, two of them
"which exact command prefixes does the operator want to allow" and "what is the
exact one-paste install command string and the token value to reuse". The
engine had both: it derives the prefixes from the plan's own steps (§17.1189)
and composes that install line, token included, in every repair hint it prints.
A typed answer can also disagree with the registry and strand the runner on a
token nobody has (the §17.1177 failure, from the other end).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import engine_setup as es


def _db(rows=None):
    db = MagicMock()
    rows = rows or {}

    async def execute(stmt, params=None):
        sql = " ".join(str(stmt).split())
        res = MagicMock()
        if "FROM assist_sessions" in sql:
            res.mappings.return_value.first.return_value = rows.get("session")
        elif "FROM jobs j" in sql or "FROM jobs" in sql:
            res.first.return_value = rows.get("job")
        else:
            res.first.return_value = None
            res.mappings.return_value.first.return_value = None
        return res

    db.execute = AsyncMock(side_effect=execute)
    return db


LEDGER = {"metadata": {"environment": {
    "profile": "Operator runs commands as root@pve in ONE interactive shell.",
    "system_state": {"host": {"kind": "host", "attrs": {"ip": "192.168.1.156"}}}}}}


@pytest.mark.asyncio
async def test_the_runner_recipes_carry_the_facts_the_refiner_would_ask_for():
    db = _db({"session": LEDGER, "job": ("job-1",)})
    with patch.object(es, "_registered_runner_token", new=AsyncMock(return_value="tok-abc")), \
         patch.object(es, "_plan_write_prefixes", new=AsyncMock(return_value=["qm set", "pct set"])):
        out = await es.known_facts_block(db, es.BY_ID["runner_writes"])
    assert "WHAT THE ENGINE ALREADY KNOWS" in out
    assert "do not ask the operator for these" in out
    assert "root@pve" not in out and "**pve**" in out          # the host, not the shell line
    assert "tok-abc" in out and "REUSE it" in out
    assert "`qm set`, `pct set`" in out and "do not ask them to produce the list" in out
    assert "--install --port 8790 --token tok-abc" in out


@pytest.mark.asyncio
async def test_an_unrelated_recipe_gets_nothing_and_never_the_token():
    """A credential in the brief of a recipe that has nothing to do with the
    runner is noise at best and a secret in the wrong transcript at worst."""
    db = _db({"session": LEDGER, "job": ("job-1",)})
    with patch.object(es, "_registered_runner_token", new=AsyncMock(return_value="tok-abc")):
        for rid in ("queue_worker", "reranker_sidecar", "step_fsm_strict"):
            out = await es.known_facts_block(db, es.BY_ID[rid])
            assert out == "", (rid, out)


@pytest.mark.asyncio
async def test_the_secrets_recipe_names_the_secrets_file_in_its_install_line():
    db = _db({"session": LEDGER, "job": ("job-1",)})
    with patch.object(es, "_registered_runner_token", new=AsyncMock(return_value="tok-abc")), \
         patch.object(es, "_plan_write_prefixes", new=AsyncMock(return_value=["qm set"])):
        out = await es.known_facts_block(db, es.BY_ID["runner_secrets"])
    assert "--secrets-file /etc/scaffold-runner/secrets.env" in out


@pytest.mark.asyncio
async def test_a_failure_leaves_the_brief_exactly_as_it_was():
    db = MagicMock(); db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    assert await es.known_facts_block(db, es.BY_ID["runner_writes"]) == ""


@pytest.mark.asyncio
async def test_the_open_plan_job_is_the_build_not_the_setup_walkthrough():
    """The moment the operator presses 'Walk me through it', the recipe's own
    job is the NEWEST open one and it has no plan — a look-up keyed on recency
    answers about the wrong job and returns nothing. Live, that is exactly what
    dropped the prefixes out of the brief."""
    db = _db({"job": ("build-job",)})
    assert await es.open_plan_job(db) == "build-job"
    sql = " ".join(str(db.execute.await_args[0][0]).split())
    assert "setup_recipe" in sql and "status = 'pending'" in sql, sql
    assert "NOT IN ('completed', 'failed', 'cancelled')" in sql, sql


@pytest.mark.asyncio
async def test_start_recipe_sends_the_augmented_brief_through_both_doors():
    import inspect
    src = inspect.getsource(es.start_recipe)
    assert "brief = r.brief + await known_facts_block(db, r)" in src
    assert "create_ideation_job(brief" in src and "spawn_phase1_background(job_id, brief)" in src
    assert "r.brief," not in src, "one door still gets the un-augmented brief"


def test_an_open_walkthrough_no_longer_erases_what_the_engine_knows():
    """The `in_progress` line REPLACED the detector's detail, so the card
    dropped the four prefixes at the exact moment the operator was about to be
    asked for them."""
    import inspect
    src = inspect.getsource(es.list_recipes)
    assert 'f" {detail}" if detail else ""' in src, src
    assert 'status, detail = "in_progress"' not in src
