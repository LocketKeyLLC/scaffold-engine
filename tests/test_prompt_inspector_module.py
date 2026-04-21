"""Tests for app/modules/prompt_inspector.py (#9.29)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.modules import prompt_inspector


def _row(**kw):
    """Tiny row object mimicking SQLAlchemy Row attribute access."""
    return SimpleNamespace(**kw)


def _mock_db_with_rows_and_fetchone(rows=None, fetchone=None):
    """Build a db mock that can return rows via fetchall() OR a single row via fetchone()."""
    result = MagicMock()
    result.fetchall.return_value = rows or []
    result.fetchone.return_value = fetchone
    db = AsyncMock()
    db.execute.return_value = result
    return db


# ---------------------------------------------------------------------------
# list_prompts
# ---------------------------------------------------------------------------
async def test_list_prompts_returns_error_when_no_rows():
    db = _mock_db_with_rows_and_fetchone(rows=[])
    result = await prompt_inspector.list_prompts(uuid4(), db)
    assert "error" in result


async def test_list_prompts_builds_node_summary():
    job_id = uuid4()
    rows = [
        _row(
            node_key="T1", title="First", status="done", execution_order=1,
            prompt_template="original template",
            optimized_prompt="shorter version",
        ),
        _row(
            node_key="T2", title="Second", status="pending", execution_order=2,
            prompt_template=None, optimized_prompt=None,
        ),
    ]
    db = _mock_db_with_rows_and_fetchone(rows=rows)
    result = await prompt_inspector.list_prompts(job_id, db)
    assert result["node_count"] == 2
    assert result["nodes"][0]["has_template"] is True
    assert result["nodes"][0]["has_optimized"] is True
    assert result["nodes"][1]["has_template"] is False
    assert result["nodes"][1]["has_optimized"] is False


async def test_list_prompts_truncates_long_previews():
    long = "x" * 500
    rows = [_row(
        node_key="T1", title="t", status="pending", execution_order=1,
        prompt_template=long, optimized_prompt=long,
    )]
    db = _mock_db_with_rows_and_fetchone(rows=rows)
    result = await prompt_inspector.list_prompts(uuid4(), db)
    assert result["nodes"][0]["template_preview"].endswith("...")
    assert len(result["nodes"][0]["template_preview"]) == 123  # 120 + "..."


# ---------------------------------------------------------------------------
# get_prompt
# ---------------------------------------------------------------------------
async def test_get_prompt_returns_error_when_missing():
    db = _mock_db_with_rows_and_fetchone(fetchone=None)
    result = await prompt_inspector.get_prompt(uuid4(), "T99", db)
    assert "error" in result


async def test_get_prompt_returns_full_detail():
    row = _row(
        node_key="T1", title="Research", status="done", execution_order=1,
        assigned_model="qwen3:4b",
        prompt_template="orig", optimized_prompt="opt", output_text="hello world",
    )
    db = _mock_db_with_rows_and_fetchone(fetchone=row)
    result = await prompt_inspector.get_prompt(uuid4(), "T1", db)
    assert result["node_key"] == "T1"
    assert result["prompt_template"] == "orig"
    assert result["optimized_prompt"] == "opt"
    assert result["has_output"] is True


# ---------------------------------------------------------------------------
# update_prompt
# ---------------------------------------------------------------------------
async def test_update_prompt_rejects_missing_node():
    db = _mock_db_with_rows_and_fetchone(fetchone=None)
    result = await prompt_inspector.update_prompt(uuid4(), "T99", "new prompt", db)
    assert "error" in result


@pytest.mark.parametrize("bad_status", ["done", "running", "skipped"])
async def test_update_prompt_rejects_non_editable_statuses(bad_status):
    row = _row(status=bad_status, optimized_prompt="old", prompt_template="orig")
    db = _mock_db_with_rows_and_fetchone(fetchone=row)
    result = await prompt_inspector.update_prompt(uuid4(), "T1", "x" * 50, db)
    assert "error" in result and bad_status in result["error"]


@pytest.mark.parametrize("ok_status", ["pending", "failed"])
async def test_update_prompt_allows_pending_and_failed(ok_status):
    row = _row(status=ok_status, optimized_prompt="old one", prompt_template="tmpl")
    db = _mock_db_with_rows_and_fetchone(fetchone=row)
    result = await prompt_inspector.update_prompt(uuid4(), "T1", "new prompt body", db)
    assert result.get("updated") is True
    assert result["old_length"] == len("old one")
    assert result["new_length"] == len("new prompt body")
    db.commit.assert_awaited()
