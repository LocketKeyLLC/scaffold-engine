"""§17.751 — the next-action guarantee on the answer path: an /ask → research
answer is content, not a step, so it ties back to the current step with the
shared ``_next_step_footer`` instead of dead-ending.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"

_ANSWER = {
    "question": "how do I set the boot order?",
    "answer": "Run `qm set 100 --boot order=scsi0`.",
    "sources": [{"kind": "web", "text": "qm set docs", "url": "https://ex/qm"}],
}


@pytest.fixture
def pipe():
    return Pipeline()


class TestNextStepFooter:
    def test_footer_present_with_step(self):
        f = _vendor._next_step_footer("T3")
        assert "👉" in f and "paste the result" in f.lower()

    def test_footer_empty_without_step(self):
        assert _vendor._next_step_footer(None) == ""


class TestResearchTieBack:
    def test_answer_ties_back_to_step(self, pipe):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, _ANSWER)
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_research_cmd(
                pipe, _SID, _ANSWER["question"], node_key="T3",
            ))
        assert "qm set" in out          # the answer rendered
        assert "👉" in out              # …and it points back to the step
        assert "paste the result for the current step" in out.lower()

    def test_no_footer_without_a_current_step(self, pipe):
        # No node_key and no chat_id → nothing to point at → bare answer.
        sess = MagicMock()
        sess.post.return_value = _make_response(200, _ANSWER)
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_research_cmd(pipe, _SID, _ANSWER["question"]))
        assert "qm set" in out
        assert "👉" not in out

    def test_valve_off_restores_bare_answer(self, pipe):
        pipe.valves.assist_answer_tieback = False
        sess = MagicMock()
        sess.post.return_value = _make_response(200, _ANSWER)
        with patch.object(_vendor, "_ss", return_value=sess):
            out = "".join(_vendor.assist_research_cmd(
                pipe, _SID, _ANSWER["question"], node_key="T3",
            ))
        assert "qm set" in out
        assert "👉" not in out
