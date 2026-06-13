"""§17.486 — pipeline-side tests for the Assist Mode guidance layer.

Covers the /assist guide + /assist research dispatch, the auto-guide trigger
on /assist next, and the render_guidance / render_research formatters. The
orchestrator HTTP calls are stubbed (no live services). Run with --noconftest.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod as _router_mod

_vendor = _router_mod._assist
_SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def pipe():
    return Pipeline()


def _drive(pipe, msg):
    return "".join(pipe._handle_assist(msg, body=None))


# ── dispatch ──────────────────────────────────────────────────────────────


class TestGuideResearchDispatch:

    def test_guide_routes_with_refine_hint(self, pipe):
        calls = []

        def _stub(pipe_arg, sid, *, node_key=None, refine=None, research=None,
                  force=True, chat_id=None):
            calls.append({"sid": sid, "refine": refine, "force": force})
            yield "STUB_GUIDE"

        with patch.object(_vendor, "assist_guide_cmd", side_effect=_stub):
            out = _drive(pipe, f"/assist guide {_SID} redo for macOS")
        assert "STUB_GUIDE" in out
        assert len(calls) == 1
        assert calls[0]["sid"] == _SID
        assert calls[0]["refine"] == "redo for macOS"
        assert calls[0]["force"] is True  # explicit /assist guide always regenerates

    def test_research_routes_with_question(self, pipe):
        calls = []

        def _stub(pipe_arg, sid, question, *, node_key=None, chat_id=None):
            calls.append({"sid": sid, "question": question})
            yield "STUB_RESEARCH"

        with patch.object(_vendor, "assist_research_cmd", side_effect=_stub):
            out = _drive(pipe, f"/assist research {_SID} which nginx flag enables gzip")
        assert "STUB_RESEARCH" in out
        assert calls[0]["sid"] == _SID
        assert calls[0]["question"] == "which nginx flag enables gzip"

    def test_research_empty_question_shows_usage(self, pipe):
        with patch.object(_vendor, "assist_research_cmd", side_effect=AssertionError):
            out = _drive(pipe, f"/assist research {_SID}")
        assert "Usage:" in out


# ── auto-guide trigger on /assist next ─────────────────────────────────────


class TestAutoGuideTrigger:

    def _stub_next_session(self, step_body):
        sess = MagicMock()
        sess.get.return_value = _make_response(200, step_body)
        return sess

    def test_next_triggers_guide_when_enabled(self, pipe):
        pipe.valves.assist_auto_guide = True
        step = {"session_id": _SID, "node_key": "T2", "title": "x",
                "tool": "LLM", "domain": "eng", "depends_on": [], "base_prompt": "bp"}
        guide_calls = []

        def _stub_guide(pipe_arg, sid, *, node_key=None, research=None,
                        force=True, chat_id=None):
            guide_calls.append({"node_key": node_key, "force": force})
            yield "WALKTHROUGH"

        with patch.object(_vendor, "_ss", return_value=self._stub_next_session(step)), \
             patch.object(_vendor, "assist_guide_cmd", side_effect=_stub_guide):
            out = "".join(_vendor.assist_next(pipe, _SID, chat_id=None))
        assert "WALKTHROUGH" in out
        assert guide_calls == [{"node_key": "T2", "force": False}]  # cache-aware

    def test_next_skips_guide_when_disabled(self, pipe):
        pipe.valves.assist_auto_guide = False
        step = {"session_id": _SID, "node_key": "T2", "title": "x",
                "tool": "LLM", "domain": "eng", "depends_on": [], "base_prompt": "bp"}

        with patch.object(_vendor, "_ss", return_value=self._stub_next_session(step)), \
             patch.object(_vendor, "assist_guide_cmd", side_effect=AssertionError):
            out = "".join(_vendor.assist_next(pipe, _SID, chat_id=None))
        assert "T2" in out  # step still rendered, just no walkthrough


# ── formatters ─────────────────────────────────────────────────────────────


class TestRenderGuidance:

    def test_ready_with_sources(self):
        out = _vendor.render_guidance({
            "node_key": "T2", "status": "ready",
            "guidance": "## Run this\n1. apt install nginx",
            "guidance_meta": {"research_sources": [
                {"kind": "searxng", "query": "nginx install"}]},
            "cached": False,
        })
        assert "How to do this step" in out
        assert "apt install nginx" in out
        assert "Confirmed via research" in out
        assert "nginx install" in out

    def test_cached_marker(self):
        out = _vendor.render_guidance({
            "node_key": "T2", "status": "ready", "guidance": "do it",
            "guidance_meta": {}, "cached": True,
        })
        assert "cached" in out.lower()

    def test_failed_degrades_gracefully(self):
        out = _vendor.render_guidance({
            "node_key": "T2", "status": "failed", "guidance": "",
            "guidance_meta": {}, "cached": False,
        })
        assert "Couldn't generate" in out
        assert "raw task prompt" in out


class TestRenderResearch:

    def test_with_sources_and_answer(self):
        out = _vendor.render_research({
            "question": "which flag?", "answer": "Use --gzip [1].",
            "sources": [{"kind": "searxng", "text": "nginx gzip docs"}],
        })
        assert "Research: which flag?" in out
        assert "Use --gzip [1]." in out
        assert "Sources:" in out
        assert "nginx gzip docs" in out

    def test_no_sources(self):
        out = _vendor.render_research({"question": "obscure", "sources": []})
        assert "No results found" in out
