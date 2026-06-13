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


# ── §17.487: env + fix dispatch ─────────────────────────────────────────────


class TestEnvFixDispatch:

    def test_env_no_args_shows_current(self, pipe):
        calls = []

        def _stub(pipe_arg, sid, *, profile=None, substitutions=None, show=False, chat_id=None):
            calls.append({"show": show}); yield "ENV_SHOW"

        with patch.object(_vendor, "assist_env_cmd", side_effect=_stub):
            out = _drive(pipe, f"/assist env {_SID}")
        assert "ENV_SHOW" in out
        assert calls[0]["show"] is True

    def test_env_parses_profile_and_substitution(self, pipe):
        calls = []

        def _stub(pipe_arg, sid, *, profile=None, substitutions=None, show=False, chat_id=None):
            calls.append({"profile": profile, "subs": substitutions}); yield "ENV_SET"

        with patch.object(_vendor, "assist_env_cmd", side_effect=_stub):
            out = _drive(pipe, f"/assist env {_SID} Ubuntu 24.04 HOST_IP=10.0.0.5")
        assert "ENV_SET" in out
        assert calls[0]["subs"] == {"HOST_IP": "10.0.0.5"}
        assert "Ubuntu 24.04" in calls[0]["profile"]
        assert "HOST_IP" not in calls[0]["profile"]  # pair stripped from profile text

    def test_fix_routes_with_error_text(self, pipe):
        calls = []

        def _stub(pipe_arg, sid, error_text, *, node_key=None, chat_id=None):
            calls.append(error_text); yield "FIX_OUT"

        with patch.object(_vendor, "assist_fix_cmd", side_effect=_stub):
            out = _drive(pipe, f"/assist fix {_SID} bash: nginx: command not found")
        assert "FIX_OUT" in out
        assert calls[0] == "bash: nginx: command not found"

    def test_fix_empty_shows_usage(self, pipe):
        with patch.object(_vendor, "assist_fix_cmd", side_effect=AssertionError):
            out = _drive(pipe, f"/assist fix {_SID}")
        assert "Usage:" in out


# ── §17.487: render_fix + render_environment ────────────────────────────────


class TestRenderFixEnv:

    def test_render_fix_ready(self):
        out = _vendor.render_fix({
            "node_key": "T2", "status": "ready",
            "fix": "## Diagnosis\nmissing pkg\n## Fix\napt install nginx",
            "guidance_meta": {"research_sources": [{"kind": "searxng", "query": "nginx 404"}]},
        })
        assert "Troubleshooting" in out
        assert "apt install nginx" in out
        assert "Confirmed via research" in out

    def test_render_fix_failed(self):
        out = _vendor.render_fix({"node_key": "T2", "status": "failed", "fix": ""})
        assert "Couldn't generate a fix" in out

    def test_render_environment_empty_nudges(self):
        out = _vendor.render_environment({"profile": "", "substitutions": {}})
        assert "No environment set" in out

    def test_render_environment_shows_values(self):
        out = _vendor.render_environment({"profile": "Ubuntu", "substitutions": {"HOST_IP": "10.0.0.5"}})
        assert "Ubuntu" in out
        assert "HOST_IP" in out and "10.0.0.5" in out


# ── §17.487: submit verdict rendering ───────────────────────────────────────


class TestSubmitVerdictRender:

    def _post_session(self, body):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, body)
        return sess

    def test_submit_warns_on_failed_verdict(self, pipe):
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "success_verdict": {"outcome": "failed", "reason": "Traceback present"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "boom", chat_id=None))
        assert "committed" in out                  # still advanced (warn mode)
        assert "may have failed" in out
        assert "Traceback present" in out

    def test_submit_block_path_not_advanced(self, pipe):
        body = {"status": "verification_failed", "no_op": False, "committed": False,
                "next_node_key": None,
                "success_verdict": {"outcome": "failed", "reason": "exit 1", "suggestion": "retry"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "boom", chat_id=None))
        assert "not marked done" in out
        assert "exit 1" in out
        assert "/assist fix" in out

    def test_submit_quiet_on_success_verdict(self, pipe):
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "success_verdict": {"outcome": "succeeded", "reason": "ok"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "committed" in out
        assert "may have failed" not in out


# ── §17.490: learned-substitutions surfacing on submit ──────────────────────


class TestSubmitLearnedSubstitutions:

    def _post_session(self, body):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, body)
        return sess

    def test_submit_surfaces_learned_values(self, pipe):
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "learned_substitutions": {"HOST_IP": "10.0.0.5"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "Learned for later steps" in out
        assert "HOST_IP" in out and "10.0.0.5" in out

    def test_submit_no_learned_no_banner(self, pipe):
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "Learned for later steps" not in out


# ── §17.491: sandbox-grounded verdict rendering ─────────────────────────────


class TestSubmitSandboxVerdictRender:

    def _post_session(self, body):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, body)
        return sess

    def test_warn_failed_sandbox_says_ran(self, pipe):
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "success_verdict": {"outcome": "failed", "reason": "NameError: foo",
                                    "grounded_by": "sandbox"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "code", chat_id=None))
        assert "Ran your code in the sandbox" in out
        assert "NameError" in out

    def test_succeeded_sandbox_shows_verified(self, pipe):
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "success_verdict": {"outcome": "succeeded", "reason": "ok",
                                    "grounded_by": "sandbox+model"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "code", chat_id=None))
        assert "Verified by running your code in the sandbox" in out

    def test_block_sandbox_says_ran(self, pipe):
        body = {"status": "verification_failed", "no_op": False, "committed": False,
                "next_node_key": None,
                "success_verdict": {"outcome": "failed", "reason": "exit 1 traceback",
                                    "grounded_by": "sandbox", "suggestion": "fix it"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "code", chat_id=None))
        assert "Ran your code in the sandbox" in out
        assert "not marked done" in out


# ── §17.492: destructive-command banner ─────────────────────────────────────


class TestDestructiveBanner:

    def test_guidance_prepends_banner(self):
        out = _vendor.render_guidance({
            "node_key": "T2", "status": "ready",
            "guidance": "## Run this\nrm -rf /opt/old",
            "guidance_meta": {"destructive": [
                {"line": "rm -rf /opt/old", "why": "recursive/forced file deletion (rm -rf)"}]},
            "cached": False,
        })
        assert "Destructive commands detected" in out
        # banner comes before the walkthrough body
        assert out.index("Destructive commands detected") < out.index("How to do this step")
        assert "back up anything important" in out

    def test_guidance_no_banner_when_clean(self):
        out = _vendor.render_guidance({
            "node_key": "T2", "status": "ready", "guidance": "ls -la",
            "guidance_meta": {"destructive": []}, "cached": False,
        })
        assert "Destructive commands detected" not in out

    def test_fix_prepends_banner(self):
        out = _vendor.render_fix({
            "node_key": "T2", "status": "ready",
            "fix": "## Fix\nmkfs.ext4 /dev/sdb1",
            "guidance_meta": {"destructive": [
                {"line": "mkfs.ext4 /dev/sdb1", "why": "format filesystem (mkfs)"}]},
        })
        assert "Destructive commands detected" in out
        assert out.index("Destructive commands detected") < out.index("Troubleshooting")
