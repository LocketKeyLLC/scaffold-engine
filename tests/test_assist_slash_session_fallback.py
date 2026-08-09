"""§17.740 — `/assist <sub>` slash commands resolve the session via the
chat_id-independent /work fallback when there's no explicit id and no chatmap.

This host's OWUI sends NO chat_id, so the chatmap recall always returns None.
Before §17.740, `/assist next` (and submit/skip/…) then demanded an explicit
session id — even though exactly one session was active and the NL path + the
top-level `/next` already resumed it via `_sole_active_session_via_work`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline, _mod

_ah = _mod._assist


@pytest.fixture
def pipe():
    return Pipeline()


_SID = "c308ae02-5ea1-4e35-9df0-476d62d5e341"
_WORK = {"session_id": _SID, "last_node_key": "ADD1", "status": "active"}


def test_resolve_session_id_prefers_explicit_uuid(pipe):
    with patch.object(_ah, "assist_recall", return_value=None), \
         patch.object(pipe, "_sole_active_session_via_work", return_value=_WORK):
        sid, rest = _ah.resolve_session_id(pipe, [_SID, "x"], None)
    assert sid == _SID and rest == ["x"]


def test_resolve_session_id_uses_chatmap_when_present(pipe):
    with patch.object(_ah, "assist_recall", return_value={"session_id": "from-map"}), \
         patch.object(pipe, "_sole_active_session_via_work", return_value=_WORK) as work:
        sid, _ = _ah.resolve_session_id(pipe, [], "chat-1")
    assert sid == "from-map"
    work.assert_not_called()   # chatmap wins over the work fallback


def test_resolve_session_id_falls_back_to_work_when_no_chatid(pipe):
    # §17.740 — the reported case: no chat_id, empty chatmap → resolve the sole
    # active session from /work instead of demanding an explicit id.
    with patch.object(_ah, "assist_recall", return_value=None), \
         patch.object(pipe, "_sole_active_session_via_work", return_value=_WORK):
        sid, _ = _ah.resolve_session_id(pipe, [], None)
    assert sid == _SID


def test_resolve_session_id_none_when_no_active_session(pipe):
    with patch.object(_ah, "assist_recall", return_value=None), \
         patch.object(pipe, "_sole_active_session_via_work", return_value=None):
        sid, _ = _ah.resolve_session_id(pipe, [], None)
    assert sid is None


def test_resolve_session_id_failsoft_on_work_error(pipe):
    with patch.object(_ah, "assist_recall", return_value=None), \
         patch.object(pipe, "_sole_active_session_via_work", side_effect=RuntimeError("boom")):
        sid, _ = _ah.resolve_session_id(pipe, [], None)
    assert sid is None   # never raises


def test_slash_recall_node_key_falls_back_to_work_current_step(pipe):
    with patch.object(_ah, "assist_recall", return_value=None), \
         patch.object(pipe, "_sole_active_session_via_work", return_value=_WORK):
        nk = _ah._slash_recall_node_key(pipe, None, None)
    assert nk == "ADD1"


def test_plain_recall_node_key_does_NOT_use_work_fallback(pipe):
    # §17.740 — the NL-path helper is unchanged (no work fallback), so its
    # "no node -> advance" behavior is preserved.
    with patch.object(_ah, "assist_recall", return_value=None), \
         patch.object(pipe, "_sole_active_session_via_work", return_value=_WORK) as work:
        nk = _ah._recall_node_key(pipe, None, None)
    assert nk is None
    work.assert_not_called()


def test_assist_next_slash_resumes_via_work_fallback(pipe):
    # End-to-end: `/assist next` with no id + no chat_id → dispatch resolves the
    # sole active session and presents it (no "requires session id" message).
    with patch.object(_ah, "assist_recall", return_value=None), \
         patch.object(pipe, "_sole_active_session_via_work", return_value=_WORK), \
         patch.object(_ah, "assist_next",
                      return_value=iter(["PRESENTED-NEXT"])) as nxt:
        out = "".join(_ah.dispatch_assist_sub(pipe, "next", [], "", chat_id=None))
    assert "PRESENTED-NEXT" in out
    assert "No active assist session" not in out
    assert nxt.call_args.args[1] == _SID   # resolved session id passed through
