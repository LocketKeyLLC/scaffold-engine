"""Audit items #7.8 (prompt revision history) + #7.9 (structured model)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.modules.prompt_inspector import update_prompt, get_history


def _row(**kw):
    m = MagicMock()
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def _node_lookup_result(status="pending", optimized="OLD", template=None):
    r = MagicMock()
    r.fetchone.return_value = _row(
        status=status, optimized_prompt=optimized, prompt_template=template
    )
    return r


def _max_rev_result(value):
    r = MagicMock()
    r.scalar.return_value = value
    return r


@pytest.mark.asyncio
async def test_first_edit_creates_revision_1():
    job_id, node = uuid4(), "T1"
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _node_lookup_result(optimized="ORIGINAL"),
        _max_rev_result(0),
        MagicMock(),  # INSERT prompt_revisions
        MagicMock(),  # UPDATE dag_nodes
    ])
    db.commit = AsyncMock()

    result = await update_prompt(job_id, node, "NEW PROMPT", db)

    assert result["updated"] is True
    assert result["revision_number"] == 1
    assert result["old_length"] == len("ORIGINAL")
    assert result["new_length"] == len("NEW PROMPT")
    # 4 SQL calls: SELECT node, SELECT max_rev, INSERT revision, UPDATE node
    assert db.execute.await_count == 4
    # The 3rd call is the INSERT — verify revision number + prompt_text
    insert_args = db.execute.await_args_list[2].args
    params = insert_args[1]
    assert params["rev"] == 1
    assert params["prompt"] == "ORIGINAL"
    assert params["source"] == "manual"


@pytest.mark.asyncio
async def test_subsequent_edit_increments_revision():
    job_id, node = uuid4(), "T2"
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _node_lookup_result(optimized="V3 TEXT"),
        _max_rev_result(2),  # already 2 revisions on file
        MagicMock(),
        MagicMock(),
    ])
    db.commit = AsyncMock()

    result = await update_prompt(job_id, node, "V4 TEXT", db)

    assert result["revision_number"] == 3
    params = db.execute.await_args_list[2].args[1]
    assert params["rev"] == 3
    assert params["prompt"] == "V3 TEXT"


@pytest.mark.asyncio
async def test_empty_old_prompt_skips_revision_insert():
    """If the node had no prompt yet, there's nothing to archive."""
    job_id, node = uuid4(), "T3"
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _node_lookup_result(optimized=None, template=None),
        _max_rev_result(0),
        MagicMock(),  # only the UPDATE — no INSERT
    ])
    db.commit = AsyncMock()

    result = await update_prompt(job_id, node, "FIRST PROMPT", db)

    assert result["revision_number"] == 0  # signals "no archive"
    assert db.execute.await_count == 3  # SELECT node, SELECT max_rev, UPDATE


@pytest.mark.asyncio
async def test_status_guard_still_works():
    job_id, node = uuid4(), "T4"
    db = MagicMock()
    db.execute = AsyncMock(return_value=_node_lookup_result(status="running"))
    db.commit = AsyncMock()

    result = await update_prompt(job_id, node, "X", db)

    assert "error" in result
    assert "running" in result["error"]
    # Only the SELECT ran — guard short-circuited.
    assert db.execute.await_count == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_source_rejected():
    db = MagicMock()
    db.execute = AsyncMock()
    result = await update_prompt(uuid4(), "T1", "NEW", db, source="hacker")
    assert "error" in result
    assert "source" in result["error"]
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_history_returns_newest_first():
    job_id, node = uuid4(), "T5"
    db = MagicMock()
    node_res = MagicMock()
    node_res.fetchone.return_value = _row(
        optimized_prompt="CURRENT", prompt_template=None
    )
    rev_res = MagicMock()
    rev_res.fetchall.return_value = [
        _row(revision_number=3, prompt_text="V3", edited_at="2026-04-27",
             edited_by="adam", source="manual"),
        _row(revision_number=2, prompt_text="V2", edited_at="2026-04-26",
             edited_by=None, source="optimizer"),
        _row(revision_number=1, prompt_text="V1", edited_at="2026-04-25",
             edited_by=None, source="initial"),
    ]
    db.execute = AsyncMock(side_effect=[node_res, rev_res])

    result = await get_history(job_id, node, db)

    assert result["current_prompt"] == "CURRENT"
    assert result["revision_count"] == 3
    assert [r["revision_number"] for r in result["revisions"]] == [3, 2, 1]
    assert result["revisions"][0]["source"] == "manual"
    assert result["revisions"][1]["source"] == "optimizer"


@pytest.mark.asyncio
async def test_get_history_unknown_node():
    db = MagicMock()
    missing = MagicMock()
    missing.fetchone.return_value = None
    db.execute = AsyncMock(return_value=missing)
    result = await get_history(uuid4(), "T_NONE", db)
    assert "error" in result
