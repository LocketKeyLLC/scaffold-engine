"""§17.779 — read endpoints backing the /ui operator SPA additions.

Covers ``node_editor.list_nodes`` (the plan editor's editable node read, incl.
``edit_version`` + JSONB ``tool_config`` normalization) and the
``jobs._json_obj`` JSONB coercion helper behind ``GET /jobs/{job_id}``.

Module-level pure/branch coverage with a mocked db — the live SQL round-trip is
verified against the running orchestrator (see the §17.779 OVERVIEW entry),
matching the existing test_node_editor.py convention.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import node_editor
from app.routers import jobs as jobs_router

_JOB_ID = "11111111-1111-1111-1111-111111111111"


def _result_first(value):
    r = MagicMock()
    r.mappings.return_value.first.return_value = value
    return r


def _result_all(values):
    r = MagicMock()
    r.mappings.return_value.all.return_value = values
    return r


def _row(**over):
    base = {
        "node_key": "T1", "title": "a", "description": None, "status": "pending",
        "depends_on": ["T0"], "execution_order": 0, "edit_version": 2,
        "prompt_template": "p", "assigned_model": "m", "tool": "LLM",
        "is_deliverable": False, "tool_config": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# node_editor.list_nodes
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestListNodes:
    async def test_malformed_uuid_400(self):
        out = await node_editor.list_nodes("not-a-uuid", AsyncMock())
        assert out["http_status"] == 400 and "error" in out

    async def test_job_not_found_404(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_result_first(None))
        out = await node_editor.list_nodes(_JOB_ID, db)
        assert out["http_status"] == 404

    async def test_returns_full_editable_payload(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result_first({"status": "planning"}),
            _result_all([_row(edit_version=5)]),
        ])
        out = await node_editor.list_nodes(_JOB_ID, db)
        assert out["job_id"] == _JOB_ID
        assert out["job_status"] == "planning"
        n = out["nodes"][0]
        # exactly the columns PATCH accepts + the optimistic-lock token
        for f in ("prompt_template", "assigned_model", "tool", "depends_on",
                  "is_deliverable", "edit_version"):
            assert f in n
        assert n["edit_version"] == 5

    async def test_tool_config_string_normalized_to_dict(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result_first({"status": "executing"}),
            _result_all([_row(tool_config='{"server": "s", "tool": "t"}')]),
        ])
        out = await node_editor.list_nodes(_JOB_ID, db)
        assert out["nodes"][0]["tool_config"] == {"server": "s", "tool": "t"}

    async def test_tool_config_bad_string_and_null_deps(self):
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _result_first({"status": "planning"}),
            _result_all([_row(tool_config="{not json", depends_on=None)]),
        ])
        out = await node_editor.list_nodes(_JOB_ID, db)
        n = out["nodes"][0]
        assert n["tool_config"] is None
        assert n["depends_on"] == []


# ---------------------------------------------------------------------------
# jobs._json_obj — JSONB coercion behind GET /jobs/{job_id}
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestJsonObj:
    def test_none_passthrough(self):
        assert jobs_router._json_obj(None) is None

    def test_dict_passthrough(self):
        assert jobs_router._json_obj({"a": 1}) == {"a": 1}

    def test_json_string_parsed(self):
        assert jobs_router._json_obj('{"a": 1}') == {"a": 1}

    def test_bad_string_is_none(self):
        assert jobs_router._json_obj("not json") is None

    def test_non_dict_json_is_none(self):
        # only object shapes are kept; a JSON array is not a brief/metadata dict
        assert jobs_router._json_obj("[1, 2]") is None
