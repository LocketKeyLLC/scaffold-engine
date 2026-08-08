"""§17.486 — pipeline-side tests for the Assist Mode guidance layer.

Covers the /assist guide + /assist research dispatch, the auto-guide trigger
on /assist next, and the render_guidance / render_research formatters. The
orchestrator HTTP calls are stubbed (no live services). Run with --noconftest.
"""
import json
import time
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

        # §17.493 — assist_stream defaults on → /assist guide routes to the stream cmd.
        with patch.object(_vendor, "assist_guide_stream_cmd", side_effect=_stub):
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

    def test_next_streams_guide_by_default(self, pipe):
        # §17.493 — assist_stream defaults on → auto-guide uses the stream cmd.
        pipe.valves.assist_auto_guide = True
        pipe.valves.assist_stream = True
        step = {"session_id": _SID, "node_key": "T2", "title": "x",
                "tool": "LLM", "domain": "eng", "depends_on": [], "base_prompt": "bp"}
        calls = []

        def _stub_stream(pipe_arg, sid, *, node_key=None, research=None,
                         force=True, chat_id=None):
            calls.append({"node_key": node_key, "force": force})
            yield "STREAMED"

        with patch.object(_vendor, "_ss", return_value=self._stub_next_session(step)), \
             patch.object(_vendor, "assist_guide_stream_cmd", side_effect=_stub_stream), \
             patch.object(_vendor, "assist_guide_cmd", side_effect=AssertionError):
            out = "".join(_vendor.assist_next(pipe, _SID, chat_id=None))
        assert "STREAMED" in out
        assert calls == [{"node_key": "T2", "force": False}]  # cache-aware

    def test_next_uses_nonstream_when_assist_stream_off(self, pipe):
        pipe.valves.assist_auto_guide = True
        pipe.valves.assist_stream = False
        step = {"session_id": _SID, "node_key": "T2", "title": "x",
                "tool": "LLM", "domain": "eng", "depends_on": [], "base_prompt": "bp"}
        calls = []

        def _stub_guide(pipe_arg, sid, *, node_key=None, research=None,
                        force=True, chat_id=None):
            calls.append({"node_key": node_key, "force": force})
            yield "WALKTHROUGH"

        with patch.object(_vendor, "_ss", return_value=self._stub_next_session(step)), \
             patch.object(_vendor, "assist_guide_cmd", side_effect=_stub_guide), \
             patch.object(_vendor, "assist_guide_stream_cmd", side_effect=AssertionError):
            out = "".join(_vendor.assist_next(pipe, _SID, chat_id=None))
        assert "WALKTHROUGH" in out
        assert "Generating walkthrough" in out  # placeholder only on the non-stream path
        assert calls == [{"node_key": "T2", "force": False}]

    def test_next_skips_guide_when_disabled(self, pipe):
        pipe.valves.assist_auto_guide = False
        step = {"session_id": _SID, "node_key": "T2", "title": "x",
                "tool": "LLM", "domain": "eng", "depends_on": [], "base_prompt": "bp"}

        with patch.object(_vendor, "_ss", return_value=self._stub_next_session(step)), \
             patch.object(_vendor, "assist_guide_stream_cmd", side_effect=AssertionError), \
             patch.object(_vendor, "assist_guide_cmd", side_effect=AssertionError):
            out = "".join(_vendor.assist_next(pipe, _SID, chat_id=None))
        assert "T2" in out  # step still rendered, just no walkthrough


# ── §17.704: visible progress while the first token is pending ──────────────


class TestGuideStreamProgress:
    """A research-heavy step is silent server-side (Milvus rerank + web fetch)
    before the first delta. The stream consumer must surface that wait instead
    of emitting an invisible ZWSP, so the operator doesn't read it as a hang."""

    @staticmethod
    def _sse(pipe):
        return type(pipe).pipe.__globals__["_SSE"]

    def test_progress_notice_and_trail_before_first_token(self, pipe):
        SSE = self._sse(pipe)
        pipe.valves.keepalive_interval = 0.05  # short so Empty ticks are cheap

        def fake_reader(url, body, q, *, stop_event=None, r_holder=None):
            time.sleep(0.16)  # simulate the research pre-pass silence (≥2 ticks)
            q.put(("event", SSE.ASSIST_GUIDE_DELTA,
                   json.dumps({"text": "Run `pveversion`."})))
            q.put(("event", SSE.ASSIST_GUIDE_DONE,
                   json.dumps({"status": "ready", "guidance_meta": {}, "cached": False})))
            q.put(("done", None, None))

        with patch.object(pipe, "_stream_sse_to_queue", side_effect=fake_reader):
            out = "".join(_vendor.assist_guide_stream_cmd(pipe, _SID, node_key="T1"))

        assert "Preparing this step" in out          # one-time notice fired
        assert "elapsed" in out                       # elapsed trail on later ticks
        assert "Run `pveversion`." in out             # content still streamed after
        assert "How to do this step" in out           # heading rendered normally

    def test_no_progress_when_first_token_is_immediate(self, pipe):
        # Self-gating: a fast/cached step yields its first delta before the
        # keepalive timeout, so the notice never fires. A large keepalive means
        # the consumer's get() blocks until content arrives (no Empty tick).
        SSE = self._sse(pipe)
        pipe.valves.keepalive_interval = 30

        def fake_reader(url, body, q, *, stop_event=None, r_holder=None):
            q.put(("event", SSE.ASSIST_GUIDE_DELTA, json.dumps({"text": "Immediate."})))
            q.put(("event", SSE.ASSIST_GUIDE_DONE,
                   json.dumps({"status": "ready", "guidance_meta": {}, "cached": False})))
            q.put(("done", None, None))

        with patch.object(pipe, "_stream_sse_to_queue", side_effect=fake_reader):
            out = "".join(_vendor.assist_guide_stream_cmd(pipe, _SID, node_key="T1"))

        assert "Preparing this step" not in out
        assert "elapsed" not in out
        assert "Immediate." in out


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


# ── §17.706: an env change re-renders the current step ──────────────────────


class TestEnvReRendersStep:

    @staticmethod
    def _put_session(body):
        sess = MagicMock()
        sess.put.return_value = _make_response(200, body)
        return sess

    def test_env_update_rerenders_current_step(self, pipe):
        body = {"session_id": _SID,
                "environment": {"profile": "root@pve web console", "substitutions": {}}}
        with patch.object(_vendor, "_ss", return_value=self._put_session(body)), \
             patch.object(_vendor, "_recall_node_key", return_value="T1"), \
             patch.object(_vendor, "assist_guide_stream_cmd",
                          side_effect=lambda *a, **k: iter(["REGEN"])) as regen:
            out = "".join(_vendor.assist_env_cmd(
                pipe, _SID, profile="root@pve web console", chat_id="c1"))
        assert "Environment updated" in out
        assert "Applying that to this step" in out
        assert "REGEN" in out                       # the step was re-rendered
        _, kwargs = regen.call_args
        assert kwargs.get("node_key") == "T1"
        assert kwargs.get("force") is True          # bypasses the stale guidance cache

    def test_env_update_no_live_step_just_confirms(self, pipe):
        body = {"session_id": _SID, "environment": {"profile": "x", "substitutions": {}}}
        with patch.object(_vendor, "_ss", return_value=self._put_session(body)), \
             patch.object(_vendor, "_recall_node_key", return_value=None), \
             patch.object(_vendor, "assist_guide_stream_cmd", side_effect=AssertionError):
            out = "".join(_vendor.assist_env_cmd(pipe, _SID, profile="x", chat_id=None))
        assert "Environment updated" in out
        assert "Applying that to this step" not in out   # nothing to re-render

    def test_env_show_does_not_rerender(self, pipe):
        body = {"session_id": _SID, "environment": {"profile": "x", "substitutions": {}}}
        sess = MagicMock()
        sess.get.return_value = _make_response(200, body)
        with patch.object(_vendor, "_ss", return_value=sess), \
             patch.object(_vendor, "_recall_node_key", return_value="T1"), \
             patch.object(_vendor, "assist_guide_stream_cmd", side_effect=AssertionError):
            out = "".join(_vendor.assist_env_cmd(pipe, _SID, show=True, chat_id="c1"))
        assert "Applying that to this step" not in out   # reads are read-only


# ── §17.707: operator-input checklist ───────────────────────────────────────


class TestChecklist:

    def test_render_empty_says_nothing_outstanding(self):
        out = _vendor.render_checklist({"items": [], "provided": {}, "open_count": 0, "total": 0})
        assert "no decisions or inputs to collect" in out

    def test_render_marks_open_and_done_and_provided(self):
        d = {
            "items": [
                {"node_key": "T2", "kind": "decision", "title": "Decide storage", "done": False},
                {"node_key": "T3", "kind": "gather", "title": "Provide specs", "done": False},
                {"node_key": "T4", "kind": "decision", "title": "Decide VLANs", "done": True},
            ],
            "provided": {"HOST_IP": "10.0.0.5"},
            "open_count": 2, "total": 3,
        }
        out = _vendor.render_checklist(d)
        assert "☐ **Decide:** Decide storage" in out
        assert "☐ **Provide:** Provide specs" in out
        assert "☑ **Decide:** Decide VLANs" in out
        assert "Already handled" in out
        assert "`HOST_IP`=`10.0.0.5`" in out
        assert "2 open / 3 total" in out

    def test_render_shows_known_facts(self):
        # §17.709 — the checklist surfaces what's been learned about the system.
        d = {"items": [], "provided": {}, "open_count": 0, "total": 0,
             "facts": ["Existing Proxmox VE 9.2.6 (not fresh)"]}
        out = _vendor.render_checklist(d)
        assert "Known about your system" in out
        assert "not fresh" in out

    @pytest.mark.parametrize("msg", [
        "what do you need from me?",
        "what do you still need from me",
        "show me the checklist",
        "what inputs do you need",
        "what's left for me to provide?",
        "what do I need to decide",
    ])
    def test_checklist_phrases_match(self, msg):
        assert _vendor._looks_like_checklist_request(msg)

    @pytest.mark.parametrize("msg", [
        "what do I need to do here?",     # about the current step, not the input list
        "how do I install nginx?",
        "next",
    ])
    def test_non_checklist_phrases_dont_match(self, msg):
        assert not _vendor._looks_like_checklist_request(msg)


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
        # §17.708 — a failed verdict is recorded (warn mode, not blocked) but
        # framed as fix-first, not "✅ committed … moving on" (contradictory).
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "success_verdict": {"outcome": "failed", "reason": "Traceback present"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "boom", chat_id=None))
        assert "Recorded your evidence" in out             # recorded (warn mode)
        assert "doesn't look like it succeeded" in out     # coherent failure lead
        assert "Traceback present" in out                  # reason surfaced
        assert "/assist fix" in out                        # points at recovery
        assert "committed. Moving on" not in out           # NOT a celebratory advance

    def test_submit_block_path_not_advanced(self, pipe):
        body = {"status": "verification_failed", "no_op": False, "committed": False,
                "next_node_key": None,
                "success_verdict": {"outcome": "failed", "reason": "exit 1", "suggestion": "retry"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "boom", chat_id=None))
        assert "not marked done" in out
        assert "exit 1" in out
        assert "/assist fix" in out

    def test_submit_incomplete_path_not_advanced(self, pipe):
        # §17.731 — an 'incomplete' block renders the not-finished framing
        # (nothing broke, more to do), names what's left, and offers skip.
        body = {"status": "step_incomplete", "no_op": False, "committed": False,
                "next_node_key": None,
                "success_verdict": {
                    "outcome": "incomplete",
                    "reason": "Only the ISO was downloaded; the OS is not installed.",
                    "suggestion": "Run the installer and boot into the installed system."}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T13", "wget iso...", chat_id=None))
        assert "isn't finished yet" in out
        assert "not marked done" in out
        assert "not installed" in out                # reason surfaced
        assert "/assist skip" in out                 # override offered
        assert "committed. Moving on" not in out

    def test_submit_quiet_on_success_verdict(self, pipe):
        pipe.valves.assist_auto_advance = False  # §17.638 — verdict render only
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
        pipe.valves.assist_auto_advance = False  # §17.638 — learned-subs render only
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "learned_substitutions": {"HOST_IP": "10.0.0.5"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "Learned for later steps" in out
        assert "HOST_IP" in out and "10.0.0.5" in out

    def test_submit_no_learned_no_banner(self, pipe):
        pipe.valves.assist_auto_advance = False  # §17.638 — learned-subs render only
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "Learned for later steps" not in out

    def test_submit_surfaces_grounding_warning(self, pipe):
        # §17.710c — a contradiction with known memory surfaces a non-blocking warning.
        pipe.valves.assist_auto_advance = False
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "grounding_warning": {"reason": "assumes a fresh install but host is existing PVE 9.2.6"}}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "looks inconsistent with what I know" in out
        assert "existing PVE 9.2.6" in out
        assert "Recorded anyway" in out            # warn-only, not blocked

    def test_submit_surfaces_captured_facts(self, pipe):
        # §17.709 — durable facts distilled from the submit are surfaced.
        pipe.valves.assist_auto_advance = False
        body = {"status": "committed", "no_op": False, "next_node_key": "T3",
                "mirror_divergence": False,
                "captured_facts": ["Existing Proxmox VE 9.2.6 (not fresh)",
                                   "vmbr0 = 192.168.1.156/24"]}
        with patch.object(_vendor, "_ss", return_value=self._post_session(body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "Noted about your system" in out
        assert "not fresh" in out


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
        pipe.valves.assist_auto_advance = False  # §17.638 — verdict render only
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


# ── §17.638: auto-advance after a clean commit ──────────────────────────────


class TestSubmitAutoAdvance:
    """After a clean commit the pipeline should claim + present the NEXT step in
    the same turn — not park on the finished one (the "output is echoing"
    symptom, where every later conversational turn re-rendered the committed
    step's walkthrough)."""

    def _session(self, submit_body, next_body):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, submit_body)
        sess.get.return_value = _make_response(200, next_body)
        return sess

    def _explode_on_get(self, submit_body, why):
        sess = MagicMock()
        sess.post.return_value = _make_response(200, submit_body)
        sess.get.side_effect = AssertionError(why)
        return sess

    def test_commit_auto_advances_to_next(self, pipe):
        pipe.valves.assist_auto_advance = True
        pipe.valves.assist_auto_guide = False  # keep the next-step render simple
        submit_body = {"status": "committed", "no_op": False,
                       "next_node_key": "T3", "mirror_divergence": False}
        next_body = {"session_id": _SID, "node_key": "T3", "title": "Third step",
                     "tool": "LLM", "domain": "eng", "depends_on": [],
                     "base_prompt": "bp"}
        with patch.object(_vendor, "_ss",
                          return_value=self._session(submit_body, next_body)):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "committed" in out
        assert "Moving on to `T3`" in out           # forward-looking phrasing
        assert "Run `/assist next`" not in out      # no manual-advance hint
        assert "Third step" in out                  # next step rendered inline

    def test_no_advance_on_failed_verdict(self, pipe):
        pipe.valves.assist_auto_advance = True
        submit_body = {"status": "committed", "no_op": False,
                       "next_node_key": "T3", "mirror_divergence": False,
                       "success_verdict": {"outcome": "failed", "reason": "boom"}}
        with patch.object(_vendor, "_ss", return_value=self._explode_on_get(
                submit_body, "must not advance on a soft-fail verdict")):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "boom", chat_id=None))
        # §17.708 — recorded but framed fix-first; does NOT auto-advance
        # (the _explode_on_get guards that), points forward manually instead.
        assert "Recorded your evidence" in out
        assert "move on to `T3`" in out

    def test_no_advance_when_valve_off(self, pipe):
        pipe.valves.assist_auto_advance = False
        submit_body = {"status": "committed", "no_op": False,
                       "next_node_key": "T3", "mirror_divergence": False}
        with patch.object(_vendor, "_ss", return_value=self._explode_on_get(
                submit_body, "valve off — must not advance")):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "Next: `T3`. Run `/assist next`" in out

    def test_no_advance_when_no_next(self, pipe):
        pipe.valves.assist_auto_advance = True
        submit_body = {"status": "committed", "no_op": False,
                       "next_node_key": None, "mirror_divergence": False}
        with patch.object(_vendor, "_ss", return_value=self._explode_on_get(
                submit_body, "no next step — must not advance")):
            out = "".join(_vendor.assist_submit(pipe, _SID, "T2", "done", chat_id=None))
        assert "All steps terminal" in out

    def test_skip_auto_advances_to_next(self, pipe):
        # §17.639 — skip has the same dead-end as submit; it auto-advances too.
        pipe.valves.assist_auto_advance = True
        pipe.valves.assist_auto_guide = False
        skip_body = {"status": "skipped", "no_op": False,
                     "next_node_key": "T3", "mirror_divergence": False}
        next_body = {"session_id": _SID, "node_key": "T3", "title": "Third step",
                     "tool": "LLM", "domain": "eng", "depends_on": [],
                     "base_prompt": "bp"}
        with patch.object(_vendor, "_ss",
                          return_value=self._session(skip_body, next_body)):
            out = "".join(_vendor.assist_skip(pipe, _SID, "T2", chat_id=None))
        assert "skipped" in out
        assert "Moving on to `T3`" in out
        assert "Third step" in out

    def test_skip_no_advance_when_valve_off(self, pipe):
        pipe.valves.assist_auto_advance = False
        skip_body = {"status": "skipped", "no_op": False,
                     "next_node_key": "T3", "mirror_divergence": False}
        with patch.object(_vendor, "_ss", return_value=self._explode_on_get(
                skip_body, "valve off — skip must not advance")):
            out = "".join(_vendor.assist_skip(pipe, _SID, "T2", chat_id=None))
        assert "Next: `T3`" in out


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


# ── §17.493: streaming consumer (assist_guide_stream_cmd) ────────────────────


class TestGuideStreamConsumer:

    def _fake_queue_filler(self, frames):
        """Return a _stream_sse_to_queue replacement that pushes `frames`
        (list of 3-tuples) then a done sentinel."""
        def _fill(url, body, q, *, stop_event=None, r_holder=None):
            for fr in frames:
                q.put(fr)
            q.put(("done", None, None))
        return _fill

    def test_streams_deltas_then_banner_and_sources(self, pipe):
        import json as _json
        frames = [
            ("connected", None, None),
            ("event", "assist_guide_delta", _json.dumps({"text": "## body line\n"})),
            ("event", "assist_guide_delta", _json.dumps({"text": "more body\n"})),
            ("event", "assist_guide_done", _json.dumps({
                "status": "ready",
                "guidance_meta": {
                    "destructive": [{"line": "rm -rf x", "why": "recursive deletion"}],
                    "research_sources": [{"kind": "searxng", "query": "nginx"}],
                },
                "cached": False,
            })),
        ]
        pipe._stream_sse_to_queue = self._fake_queue_filler(frames)
        out = "".join(_vendor.assist_guide_stream_cmd(pipe, _SID, force=True))
        assert "How to do this step" in out          # header emitted on first delta
        assert "## body line" in out and "more body" in out
        assert "Destructive commands detected" in out  # trailing banner
        assert "Confirmed via research" in out         # trailing sources

    def test_cache_hit_marks_cached(self, pipe):
        import json as _json
        frames = [
            ("connected", None, None),
            ("event", "assist_guide_delta", _json.dumps({"text": "cached walk"})),
            ("event", "assist_guide_done", _json.dumps({
                "status": "ready", "guidance_meta": {}, "cached": True})),
        ]
        pipe._stream_sse_to_queue = self._fake_queue_filler(frames)
        out = "".join(_vendor.assist_guide_stream_cmd(pipe, _SID, force=False))
        assert "cached walk" in out
        assert "cached" in out.lower()

    def test_http_error_surfaced(self, pipe):
        def _fill(url, body, q, *, stop_event=None, r_holder=None):
            q.put(("http_error", 409, "session not active"))
        pipe._stream_sse_to_queue = _fill
        out = "".join(_vendor.assist_guide_stream_cmd(pipe, _SID, force=True))
        assert "HTTP 409" in out


# ── §17.499 — verbosity dispatch + render ───────────────────────────────────


class TestVerbosity:

    def test_verbose_routes_to_env_cmd(self, pipe):
        calls = []

        def _stub(pipe_arg, sid, *, profile=None, substitutions=None,
                  verbosity=None, show=False, chat_id=None):
            calls.append({"verbosity": verbosity}); yield "VERB_SET"

        with patch.object(_vendor, "assist_env_cmd", side_effect=_stub):
            out = _drive(pipe, f"/assist verbose {_SID} detailed")
        assert "VERB_SET" in out
        assert calls[0]["verbosity"] == "detailed"

    def test_verbose_rejects_bad_level(self, pipe):
        with patch.object(_vendor, "assist_env_cmd", side_effect=AssertionError):
            out = _drive(pipe, f"/assist verbose {_SID} loud")
        assert "Usage:" in out

    def test_render_environment_shows_verbosity(self):
        out = _vendor.render_environment(
            {"profile": "Ubuntu", "substitutions": {}, "verbosity": "terse"})
        assert "Verbosity" in out and "terse" in out
