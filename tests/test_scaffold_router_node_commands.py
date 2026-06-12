"""§17.479 — `/node` interactive node-control chat commands.

`_handle_node` dispatches reset / del / edit / reorder to the §17.478 /nodes
CRUD API with tiered job_id recall. Mocks the HTTP session and asserts the
endpoint + payload.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline

_JID = "01ab243e"  # 8-hex short_id — job-id-shaped per _JOB_ID_TOKEN_RE


@pytest.fixture
def pipe():
    return Pipeline()


def _resp(payload, code=200):
    r = MagicMock(status_code=code)
    r.json.return_value = payload
    r.text = str(payload)
    return r


@pytest.mark.smoke
class TestNodeCommands:
    def test_help(self, pipe):
        out = pipe._handle_node(["/node", "help"])
        assert "/node reset" in out and "/node del" in out and "/node edit" in out

    def test_bare_node_is_help(self, pipe):
        assert "interactive node control" in pipe._handle_node(["/node"]).lower()

    def test_reset_explicit(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _resp({"status": "ok", "node_key": "T2"})
            out = pipe._handle_node(["/node", "reset", _JID, "T2"])
        assert mp.call_args[0][0].endswith(f"/nodes/{_JID}/T2/reset")
        assert "ok" in out

    def test_delete_explicit(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.delete") as md:
            md.return_value = _resp({"status": "ok"})
            pipe._handle_node(["/node", "del", _JID, "T2"])
        assert md.call_args[0][0].endswith(f"/nodes/{_JID}/T2")

    def test_edit_title_joins_value(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.patch") as mp:
            mp.return_value = _resp({"status": "ok"})
            pipe._handle_node(["/node", "edit", _JID, "T1", "title", "New", "name"])
        assert mp.call_args.kwargs["json"] == {"title": "New name"}
        assert mp.call_args[0][0].endswith(f"/nodes/{_JID}/T1")

    def test_edit_deliverable_coerces_bool(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.patch") as mp:
            mp.return_value = _resp({"status": "ok"})
            pipe._handle_node(["/node", "edit", _JID, "T1", "deliverable", "true"])
        assert mp.call_args.kwargs["json"] == {"is_deliverable": True}

    def test_edit_unknown_field(self, pipe):
        out = pipe._handle_node(["/node", "edit", _JID, "T1", "bogus", "x"])
        assert "Unknown field" in out

    def test_reorder_splits_csv(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _resp({"status": "ok"})
            pipe._handle_node(["/node", "reorder", _JID, "T2,T1,T3"])
        assert mp.call_args[0][0].endswith(f"/nodes/{_JID}/reorder")
        assert mp.call_args.kwargs["json"] == {"ordered_keys": ["T2", "T1", "T3"]}

    def test_reset_recalls_active_job(self, pipe):
        with patch.object(pipe, "_active_job_recall",
                          return_value={"job_id": "RID9", "title": "t"}), \
             patch("scaffold_router._HTTP_SESSION.post") as mp:
            mp.return_value = _resp({"status": "ok"})
            pipe._handle_node(["/node", "reset", "T2"])  # node_key only
        assert mp.call_args[0][0].endswith("/nodes/RID9/T2/reset")

    def test_reset_no_recall_errors(self, pipe):
        with patch.object(pipe, "_active_job_recall", return_value=None):
            out = pipe._handle_node(["/node", "reset", "T2"])
        assert "No active job" in out
