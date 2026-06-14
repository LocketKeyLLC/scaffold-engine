"""§17.520 — `/assist status` session roll-up.

The mirror-divergence banner told operators to "Inspect with `/assist status`"
(_assist_handlers.py) but the subcommand was never implemented — a dangling
reference that fell through to the help table. This implements it (GET
/assist/{session_id} → session + step_counts) and pins the render + routing.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipe():
    return Pipeline()


_SESSION_BODY = {
    "id": _SID, "job_id": "job-xyz", "status": "active",
    "current_node_key": "T3",
    "step_counts": {"committed": 2, "presented": 1, "pending": 5},
}


class TestAssistStatus:
    def test_status_renders_rollup(self, pipe):
        sess = MagicMock()
        sess.get.return_value = _make_response(200, _SESSION_BODY)
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_status(pipe, _SID))
        assert _SID in out
        assert "active" in out
        assert "job-xyz" in out
        assert "T3" in out                 # current step
        assert "committed=2" in out        # step counts surfaced

    def test_status_404(self, pipe):
        sess = MagicMock()
        sess.get.return_value = _make_response(404, {"detail": "not found"})
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_status(pipe, _SID))
        assert "No assist session" in out

    def test_status_routes_via_dispatch(self, pipe):
        """`/assist status <sid>` reaches assist_status (not the help fallback)."""
        sess = MagicMock()
        sess.get.return_value = _make_response(200, _SESSION_BODY)
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(pipe._handle_assist(f"/assist status {_SID}", body=None))
        assert "Assist session" in out
        assert "active" in out

    def test_status_no_session_hint(self, pipe):
        # Bare `/assist status` with no recalled session → friendly usage hint,
        # not a crash or the help table.
        out = "".join(pipe._handle_assist("/assist status", body=None))
        assert "status" in out.lower()
