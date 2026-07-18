"""§17.478 (Phase 4) — interactive node-control (CRUD) module.

Pure-helper coverage for the graph logic + operation decision branches
(version conflict, validation, cascade) with `_load_nodes` patched and a mock
db — the SQL write paths are verified live (see the §17.478 OVERVIEW entry).
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import node_editor


def _db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


def _node(key, *, status="pending", deps=None, order=0, version=0, deliverable=False):
    return {
        "node_key": key, "status": status, "depends_on": deps or [],
        "execution_order": order, "edit_version": version,
        "is_deliverable": deliverable,
    }


def _patch_load(nodes):
    # side_effect supports ops (delete) that re-load the post-state.
    if isinstance(nodes, list) and nodes and isinstance(nodes[0], list):
        return patch.object(node_editor, "_load_nodes", AsyncMock(side_effect=nodes))
    return patch.object(node_editor, "_load_nodes", AsyncMock(return_value=nodes))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestGraphHelpers:
    def test_transitive_downstream(self):
        nodes = [
            _node("T1"), _node("T2", deps=["T1"]),
            _node("T3", deps=["T2"]), _node("T4", deps=["T2", "T3"]),
        ]
        assert node_editor._transitive_downstream(nodes, "T1") == {"T2", "T3", "T4"}
        assert node_editor._transitive_downstream(nodes, "T3") == {"T4"}
        assert node_editor._transitive_downstream(nodes, "T4") == set()

    def test_validate_graph_ok(self):
        assert node_editor._validate_graph({"T1": [], "T2": ["T1"]}) is None

    def test_validate_graph_unknown_ref(self):
        err = node_editor._validate_graph({"T1": ["TX"]})
        assert err and "unknown" in err

    def test_validate_graph_self_ref(self):
        err = node_editor._validate_graph({"T1": ["T1"]})
        assert err and "itself" in err

    def test_validate_graph_cycle(self):
        err = node_editor._validate_graph({"T1": ["T2"], "T2": ["T1"]})
        assert err and "cycle" in err


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestEdit:
    async def test_not_found(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.edit_node("j", "TX", {"title": "x"}, db=_db())
        assert r["http_status"] == 404

    async def test_stale_version_409(self):
        with _patch_load([_node("T1", version=3)]):
            r = await node_editor.edit_node(
                "j", "T1", {"title": "x"}, expected_version=1, db=_db())
        assert r["http_status"] == 409

    async def test_matching_version_ok(self):
        with _patch_load([_node("T1", version=3)]):
            r = await node_editor.edit_node(
                "j", "T1", {"title": "x"}, expected_version=3, db=_db())
        assert r["status"] == "ok"

    async def test_no_editable_fields_400(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.edit_node("j", "T1", {"bogus": 1}, db=_db())
        assert r["http_status"] == 400

    async def test_depends_on_cycle_400(self):
        nodes = [_node("T1"), _node("T2", deps=["T1"])]
        with _patch_load(nodes):
            r = await node_editor.edit_node("j", "T1", {"depends_on": ["T2"]}, db=_db())
        assert r["http_status"] == 400 and "cycle" in r["error"]

    async def test_invalidating_edit_resets_done_node_and_downstream(self):
        nodes = [
            _node("T1", status="done"), _node("T2", status="done", deps=["T1"]),
        ]
        with _patch_load(nodes):
            r = await node_editor.edit_node("j", "T1", {"tool": "CodeGen"}, db=_db())
        assert r["status"] == "ok"
        assert r["reset"] == ["T1", "T2"]   # node + downstream invalidated

    async def test_metadata_edit_does_not_reset(self):
        nodes = [_node("T1", status="done"), _node("T2", status="done", deps=["T1"])]
        with _patch_load(nodes):
            r = await node_editor.edit_node("j", "T1", {"title": "new"}, db=_db())
        assert r["status"] == "ok" and r["reset"] == []   # title is metadata

    async def test_prompt_template_is_editable_and_invalidating(self):
        """§17.614 (audit #11) — prompt_template (the field execution consumes)
        is editable and invalidating, so a prompt edit actually takes effect."""
        nodes = [_node("T1", status="done"), _node("T2", status="done", deps=["T1"])]
        with _patch_load(nodes):
            r = await node_editor.edit_node("j", "T1", {"prompt_template": "new prompt"}, db=_db())
        assert r["status"] == "ok"
        assert r["reset"] == ["T1", "T2"]   # invalidating → resets node + downstream

    async def test_optimized_prompt_no_longer_editable(self):
        """§17.614 (audit #11) — editing optimized_prompt alone is now a 400
        (it was a silent no-op the executor overwrote); operators aren't misled."""
        with _patch_load([_node("T1")]):
            r = await node_editor.edit_node("j", "T1", {"optimized_prompt": "x"}, db=_db())
        assert r["http_status"] == 400


# ---------------------------------------------------------------------------
# insert / delete / reorder / reset
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestInsert:
    async def test_duplicate_409(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.insert_node(
                "j", {"node_key": "T1", "title": "x"}, db=_db())
        assert r["http_status"] == 409

    async def test_unknown_dep_400(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.insert_node(
                "j", {"node_key": "T2", "title": "x", "depends_on": ["TX"]}, db=_db())
        assert r["http_status"] == 400

    async def test_happy(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.insert_node(
                "j", {"node_key": "T2", "title": "x", "depends_on": ["T1"]}, db=_db())
        assert r["status"] == "ok" and r["node_key"] == "T2"

    async def test_missing_required_400(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.insert_node("j", {"title": "no key"}, db=_db())
        assert r["http_status"] == 400


@pytest.mark.smoke
class TestDelete:
    async def test_not_found_404(self):
        with _patch_load([_node("T1"), _node("T2")]):
            r = await node_editor.delete_node("j", "TX", db=_db())
        assert r["http_status"] == 404

    async def test_last_node_400(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.delete_node("j", "T1", db=_db())
        assert r["http_status"] == 400

    async def test_rewires_and_resets_dependents(self):
        nodes = [
            _node("T1"), _node("T2", status="done", deps=["T1"]),
            _node("T3", status="done", deps=["T2"]),
        ]
        post = [_node("T1"), _node("T3", status="done", deps=[])]
        with _patch_load([nodes, post]):
            r = await node_editor.delete_node("j", "T2", db=_db())
        assert r["status"] == "ok"
        assert r["rewired"] == ["T3"]
        assert "T3" in r["reset"]


@pytest.mark.smoke
class TestReorder:
    async def test_bad_permutation_400(self):
        with _patch_load([_node("T1"), _node("T2")]):
            r = await node_editor.reorder_nodes("j", ["T1", "TX"], db=_db())
        assert r["http_status"] == 400

    async def test_happy(self):
        with _patch_load([_node("T1"), _node("T2")]):
            r = await node_editor.reorder_nodes("j", ["T2", "T1"], db=_db())
        assert r["status"] == "ok" and r["order"] == ["T2", "T1"]


@pytest.mark.smoke
class TestReset:
    async def test_not_found_404(self):
        with _patch_load([_node("T1")]):
            r = await node_editor.reset_node("j", "TX", db=_db())
        assert r["http_status"] == 404

    async def test_cascades_downstream(self):
        nodes = [
            _node("T1", status="done"), _node("T2", status="done", deps=["T1"]),
            _node("T3", status="done", deps=["T2"]),
        ]
        with _patch_load(nodes):
            r = await node_editor.reset_node("j", "T1", db=_db())
        assert r["status"] == "ok"
        assert r["reset"] == ["T1", "T2", "T3"]
        assert r["downstream_reset"] == ["T2", "T3"]


# ---------------------------------------------------------------------------
# Router dispatch (app/routers/nodes.py)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestRouterDispatch:
    async def test_bad_uuid_400(self):
        from app.routers import nodes as nr
        from app.schemas import NodeEditInput
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            await nr.node_edit("not-a-uuid", "T1", NodeEditInput(title="x"), db=_db())
        assert ei.value.status_code == 400

    async def test_error_result_maps_to_http_status(self):
        from app.routers import nodes as nr
        from app.schemas import NodeEditInput
        from fastapi import HTTPException
        _JID = "c2b18327-cde9-4842-add4-72a248d99666"
        with patch.object(nr.node_editor, "edit_node",
                          AsyncMock(return_value={"error": "stale", "http_status": 409})):
            with pytest.raises(HTTPException) as ei:
                await nr.node_edit(_JID, "T1", NodeEditInput(title="x"), db=_db())
        assert ei.value.status_code == 409

    async def test_ok_result_passthrough(self):
        from app.routers import nodes as nr
        from app.schemas import NodeResetInput
        from fastapi import HTTPException  # noqa: F401
        _JID = "c2b18327-cde9-4842-add4-72a248d99666"
        with patch.object(nr.node_editor, "reset_node",
                          AsyncMock(return_value={"status": "ok", "node_key": "T1"})):
            out = await nr.node_reset(_JID, "T1", NodeResetInput(), db=_db())
        assert out["status"] == "ok"


# ---------------------------------------------------------------------------
# §17.600 — insert_node re-opens a terminal job (audit finding #8)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_insert_node_reopens_terminal_job():
    """insert_node must re-open a terminal job so the new 'pending' node is
    scheduled — edit/delete/reset_node all do this; insert_node didn't, so the
    node never ran despite a 200 'ok'."""
    db = _db()
    existing = [_node("T1", order=0), _node("T2", deps=["T1"], order=1)]
    with _patch_load(existing):
        r = await node_editor.insert_node(
            "job-1",
            {"node_key": "T3", "title": "New", "depends_on": ["T2"]},
            db=db,
        )
    assert r.get("status") == "ok"
    sqls = [str(c.args[0]) for c in db.execute.call_args_list]
    assert any(
        "SET status = 'executing'" in s and "compiled_output = NULL" in s
        for s in sqls
    ), "insert_node did not call _reopen_job"
