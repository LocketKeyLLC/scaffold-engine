"""Unit tests for app/modules/artifacts.py (§17.565).

AsyncMock DB sessions verify the SQL choreography: idempotent reset,
job-level row, per-node CodeGen rows + output_artifact_id back-pointer,
artifact_type mapping, and the empty-compiled_output no-op.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.artifacts import persist_job_artifacts, _job_artifact_type


def _result(mappings_first=None, mappings_all=None, scalar=None):
    r = MagicMock()
    m = MagicMock()
    m.first.return_value = mappings_first
    m.all.return_value = mappings_all or []
    r.mappings.return_value = m
    r.scalar.return_value = scalar
    return r


def _sqls(db):
    return [str(c.args[0]) for c in db.execute.call_args_list]


class TestArtifactTypeMapping:
    def test_plan_only(self):
        assert _job_artifact_type("plan_only") == "plan"

    def test_executed_is_report(self):
        assert _job_artifact_type("executed") == "report"

    def test_assist_completed_is_report(self):
        assert _job_artifact_type("assist_completed") == "report"

    def test_none_is_report(self):
        assert _job_artifact_type(None) == "report"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_persist_writes_job_and_codegen_and_back_pointer():
    db = AsyncMock()
    db.execute.side_effect = [
        _result(),                                              # UPDATE reset
        _result(),                                              # DELETE artifacts
        _result(mappings_first={"title": "My Job",
                                "compiled_output": "# Deliverable"}),  # SELECT job
        _result(),                                              # INSERT job-level
        _result(mappings_all=[{"id": "n1", "title": "Gen code",
                               "output_text": "print(1)"}]),    # SELECT CodeGen nodes
        _result(scalar="art-1"),                               # INSERT per-node RETURNING id
        _result(),                                              # UPDATE output_artifact_id
    ]
    n = await persist_job_artifacts("job-1", db, deliverable_kind="executed")
    assert n == 2
    sqls = _sqls(db)
    # Idempotent reset happens BEFORE any insert.
    assert "output_artifact_id = NULL" in sqls[0]
    assert "DELETE FROM artifacts" in sqls[1]
    assert "INSERT INTO artifacts" in sqls[3]          # job-level
    # Per-node back-pointer is set.
    assert any("UPDATE dag_nodes SET output_artifact_id = :aid" in s for s in sqls)
    # Job-level row is a 'report' for an executed job.
    assert db.execute.call_args_list[3].args[1]["atype"] == "report"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_persist_plan_only_job_level_type():
    db = AsyncMock()
    db.execute.side_effect = [
        _result(), _result(),
        _result(mappings_first={"title": "X", "compiled_output": "plan body"}),
        _result(),                          # INSERT job-level
        _result(mappings_all=[]),           # no CodeGen nodes
    ]
    n = await persist_job_artifacts("job-1", db, deliverable_kind="plan_only")
    assert n == 1
    assert db.execute.call_args_list[3].args[1]["atype"] == "plan"


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_persist_no_compiled_output_is_noop():
    db = AsyncMock()
    db.execute.side_effect = [
        _result(),                          # UPDATE reset
        _result(),                          # DELETE
        _result(mappings_first={"title": "X", "compiled_output": ""}),
    ]
    n = await persist_job_artifacts("job-1", db, deliverable_kind="executed")
    assert n == 0
    # Reset+delete ran, but no INSERT.
    assert db.execute.await_count == 3
    assert not any("INSERT INTO artifacts" in s for s in _sqls(db))
