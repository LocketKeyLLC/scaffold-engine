"""§17.479 — web job-detail node-action routes (reset / delete).

These ``async def`` routes call node_editor + execution_status in-process (no
SDK loopback). Tests override get_db and patch the two module functions, then
assert the route re-renders the job-detail root fragment.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_db


@pytest.fixture(autouse=True)
def _single_user_mode(monkeypatch):
    """§17.812 — /web is auth-exempt only in single-user mode; pin it so these
    tests don't depend on the ambient MULTI_USER_ENABLED env."""
    import app.config
    monkeypatch.setattr(app.config.settings, "multi_user_enabled", False)


_UUID = "01ab243e-1234-5678-9abc-def012345678"
_PAYLOAD = {
    "job_id": "jx", "job_title": "X", "job_status": "executing",
    "counts": {"pending": 1}, "total_nodes": 1, "next_node": None,
    "compiled_output": None,
    "nodes": [{
        "node_key": "T1", "title": "x", "status": "pending",
        "execution_order": 0, "actionable": True, "is_deliverable": False,
        "confidence": None, "tool": "LLM",
    }],
}


@pytest.fixture
def web():
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.smoke
def test_web_node_reset_renders_fragment(web):
    with patch("app.modules.node_editor.reset_node",
               new=AsyncMock(return_value={"status": "ok", "reset": ["T1"]})) as mr, \
         patch("app.modules.execution_handler.execution_status",
               new=AsyncMock(return_value=_PAYLOAD)):
        resp = web.post(f"/web/jobs/{_UUID}/nodes/T1/reset")
    assert resp.status_code == 200
    assert 'id="job-detail-root"' in resp.text
    # The re-rendered table carries the §17.479 action buttons.
    assert "/reset" in resp.text and "/delete" in resp.text
    mr.assert_awaited()


@pytest.mark.smoke
def test_web_node_delete_calls_editor(web):
    with patch("app.modules.node_editor.delete_node",
               new=AsyncMock(return_value={"status": "ok"})) as md, \
         patch("app.modules.execution_handler.execution_status",
               new=AsyncMock(return_value=_PAYLOAD)):
        resp = web.post(f"/web/jobs/{_UUID}/nodes/T1/delete")
    assert resp.status_code == 200
    md.assert_awaited()


@pytest.mark.smoke
def test_web_node_reset_editor_error_still_renders(web):
    # A module-layer error (e.g. node not found) must not 500 the page — the
    # route logs it and re-renders the current state.
    with patch("app.modules.node_editor.reset_node",
               new=AsyncMock(return_value={"error": "node TX not found", "http_status": 404})), \
         patch("app.modules.execution_handler.execution_status",
               new=AsyncMock(return_value=_PAYLOAD)):
        resp = web.post(f"/web/jobs/{_UUID}/nodes/TX/reset")
    assert resp.status_code == 200
    assert 'id="job-detail-root"' in resp.text


# §17.480 — edit / insert / move web routes.
@pytest.mark.smoke
def test_web_node_edit_calls_editor(web):
    with patch("app.modules.node_editor.edit_node",
               new=AsyncMock(return_value={"status": "ok"})) as me, \
         patch("app.modules.execution_handler.execution_status",
               new=AsyncMock(return_value=_PAYLOAD)):
        resp = web.post(f"/web/jobs/{_UUID}/nodes/T1/edit",
                        data={"title": "New", "tool": "CodeGen",
                              "is_deliverable": "on", "expected_version": "0"})
    assert resp.status_code == 200
    me.assert_awaited()
    fields = me.await_args.args[2]
    assert fields["title"] == "New" and fields["is_deliverable"] is True


@pytest.mark.smoke
def test_web_node_insert_calls_editor(web):
    with patch("app.modules.node_editor.insert_node",
               new=AsyncMock(return_value={"status": "ok"})) as mi, \
         patch("app.modules.execution_handler.execution_status",
               new=AsyncMock(return_value=_PAYLOAD)):
        resp = web.post(f"/web/jobs/{_UUID}/nodes",
                        data={"node_key": "T9", "title": "New", "tool": "LLM",
                              "depends_on": "T1,T2"})
    assert resp.status_code == 200
    mi.assert_awaited()
    spec = mi.await_args.args[1]
    assert spec["node_key"] == "T9" and spec["depends_on"] == ["T1", "T2"]


@pytest.mark.smoke
def test_web_node_move_reorders(web):
    nodes = [{"node_key": "T1"}, {"node_key": "T2"}, {"node_key": "T3"}]
    with patch("app.modules.node_editor._load_nodes",
               new=AsyncMock(return_value=nodes)), \
         patch("app.modules.node_editor.reorder_nodes",
               new=AsyncMock(return_value={"status": "ok"})) as mr, \
         patch("app.modules.execution_handler.execution_status",
               new=AsyncMock(return_value=_PAYLOAD)):
        resp = web.post(f"/web/jobs/{_UUID}/nodes/T2/move?dir=up")
    assert resp.status_code == 200
    # T2 swapped above T1.
    assert mr.await_args.args[1] == ["T2", "T1", "T3"]


@pytest.mark.smoke
def test_web_node_edit_cancel_clears(web):
    resp = web.get(f"/web/jobs/{_UUID}/nodes/T1/edit/cancel")
    assert resp.status_code == 200 and resp.text == ""
