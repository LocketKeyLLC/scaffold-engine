"""§17.654 — pipeline-side notes & additions: capture + confirm-back, the
status roll-up '📌 Notes & additions' block, and the note-intent route.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipe():
    return Pipeline()


class TestAssistNoteCmd:
    def test_note_confirms_back_then_represents_current_step(self, pipe):
        # §17.750 — with a claimed step, confirming a note re-presents the
        # current step's walkthrough (copy-paste commands) instead of a bare
        # "say next" dead-end.
        pipe.valves.assist_stream = False  # exercise the non-SSE guide path
        sess = MagicMock()
        sess.post.return_value = _make_response(
            200, {"recorded": True, "note": {"kind": "addition", "text": "add a DMZ"}}
        )
        with patch.object(_vendor, "_ss", return_value=sess), \
             patch.object(_vendor, "assist_guide_cmd",
                          return_value=iter(["## Walkthrough\n```bash\necho hi\n```"])) as guide:
            out = "".join(_vendor.assist_note_cmd(
                pipe, _SID, "add a DMZ segment", kind="addition", node_key="T2",
            ))
        assert "📌 Noted (addition):" in out
        assert "add a DMZ segment" in out
        assert "carry this forward" not in out.lower()
        assert "```bash" in out  # the re-presented walkthrough
        # posted to the /note endpoint with the right body
        _, kwargs = sess.post.call_args
        assert kwargs["json"]["kind"] == "addition"
        assert kwargs["json"]["text"] == "add a DMZ segment"
        # re-presented the CURRENT step, cached (force=False), not a fresh gen
        _, gkwargs = guide.call_args
        assert gkwargs["node_key"] == "T2"
        assert gkwargs["force"] is False

    def test_note_without_step_keeps_plain_confirm(self, pipe):
        # §17.750 — no claimed step to re-present → the old carry-forward hint.
        sess = MagicMock()
        sess.post.return_value = _make_response(
            200, {"recorded": True, "note": {"kind": "addition", "text": "add a DMZ"}}
        )
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_note_cmd(
                pipe, _SID, "add a DMZ segment", kind="addition",
            ))
        assert "📌 Noted (addition):" in out
        assert "carry this forward" in out.lower()

    def test_note_represent_valve_off_keeps_plain_confirm(self, pipe):
        # §17.750 — flipping assist_note_represents_step off restores the bare
        # acknowledgement even with a claimed step.
        pipe.valves.assist_note_represents_step = False
        sess = MagicMock()
        sess.post.return_value = _make_response(
            200, {"recorded": True, "note": {"kind": "addition", "text": "add a DMZ"}}
        )
        with patch.object(_vendor, "_ss", return_value=sess), \
             patch.object(_vendor, "assist_guide_cmd") as guide:
            out = "".join(_vendor.assist_note_cmd(
                pipe, _SID, "add a DMZ segment", kind="addition", node_key="T2",
            ))
        assert "carry this forward" in out.lower()
        guide.assert_not_called()

    def test_note_empty_text_prompts_for_content(self, pipe):
        # no HTTP call when there is nothing to record
        with patch.object(_vendor, "_ss") as ss:
            out = "".join(_vendor.assist_note_cmd(pipe, _SID, "   "))
        assert "What should I note" in out
        ss.assert_not_called()

    def test_note_http_error_surfaced(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(409, {"detail": "empty note text"})
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_note_cmd(pipe, _SID, "x"))
        assert "HTTP 409" in out


class TestNotesBlockInStatus:
    def test_status_renders_notes_block(self, pipe):
        body = {
            "id": _SID, "job_id": "job-xyz", "status": "active",
            "current_node_key": "T3", "step_counts": {"pending": 5},
            "notes": [
                {"kind": "constraint", "text": "only 2 NICs", "node_key": "T2"},
                {"kind": "addition", "text": "add a DMZ", "node_key": None},
            ],
        }
        sess = MagicMock()
        sess.get.return_value = _make_response(200, body)
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_status(pipe, _SID))
        assert "Notes & additions" in out
        assert "only 2 NICs" in out
        assert "add a DMZ" in out
        assert "from `T2`" in out          # node provenance shown when present

    def test_status_no_notes_omits_block(self, pipe):
        body = {
            "id": _SID, "job_id": "j", "status": "active",
            "current_node_key": "T3", "step_counts": {}, "notes": [],
        }
        sess = MagicMock()
        sess.get.return_value = _make_response(200, body)
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_status(pipe, _SID))
        assert "Notes & additions" not in out

    def test_render_notes_block_tolerates_json_string(self):
        s = _vendor._render_notes_block('[{"kind":"note","text":"remember X"}]')
        assert "remember X" in s
        assert _vendor._render_notes_block("not json") == ""
        assert _vendor._render_notes_block(None) == ""


class TestNoteIntentRoute:
    def test_nl_turn_routes_note_intent_to_note_cmd(self, pipe):
        """A plain-language note message → /interpret intent=note → assist_note_cmd."""
        interp = {"intent": "note", "note_text": "wants a DMZ",
                  "note_kind": "addition", "node_key": "T2"}
        with patch.object(_vendor, "fast_classify_turn", return_value=None), \
             patch.object(_vendor, "assist_interpret", return_value=interp), \
             patch.object(_vendor, "_recall_node_key", return_value="T2"), \
             patch.object(_vendor, "assist_note_cmd",
                          return_value=iter(["📌 Noted (addition): wants a DMZ"])) as note_cmd:
            out = "".join(_vendor.assist_nl_turn(
                pipe, _SID, "also I want a DMZ", node_key="T2", chat_id="c1",
            ))
        assert "📌 Noted (addition):" in out
        _, kwargs = note_cmd.call_args
        assert kwargs["kind"] == "addition"
