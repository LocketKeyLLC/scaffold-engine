"""§17.628 — engine-wide natural-language command routing (pipeline side).

At the top level (no active assist session), a plain sentence that clearly
names a READ-ONLY engine action is translated to its canonical slash string and
run through the existing `_handle_command`. Two tiers mirror the in-session
assist NL layer: a deterministic fast-phrase table (no LLM) and the `/route`
classifier (LLM), which only intercepts on confidence='high' + a satisfied
required slot.

Pins:
  * `_fast_classify_command` — whole-message phrase → intent, else None.
  * `_nl_command_route` — valve gate; fast-path skips the classifier; only
    high-confidence + required-slot classifications intercept; everything else
    returns None (→ triage). This is the "won't hijack a conversation" contract.
  * `_dispatch_nl_command` — each intent maps to the right slash string.
  * `_nl_results` / `_resolve_job_ref` — unique match, ambiguous pick-list, no
    ref (recall), and no-match clarify.
  * pipe() end-to-end: a clear read is intercepted; an idea falls to triage.

Run with --noconftest (pipeline test).
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline, _make_response, _mod


@pytest.fixture
def pipe():
    return Pipeline()


def _hist(last: str) -> list[dict]:
    """Non-first-turn history with NO assist-session marker, so neither the
    welcome preamble nor history-based session recovery fires."""
    return [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "Hi — what would you like to do?"},
        {"role": "user", "content": last},
    ]


# ---------------------------------------------------------------------------
# _fast_classify_command — deterministic tier
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestFastClassify:
    @pytest.mark.parametrize("msg,intent", [
        ("what's running", "status"),
        ("What's Running!", "status"),        # case + punctuation normalized
        ("  list my jobs  ", "jobs_list"),
        ("list models", "model_list"),
        ("available models", "model_available"),
        ("probe models", "model_probe"),
        ("help", "help"),
        ("what can you do", "help"),
    ])
    def test_known_phrases(self, msg, intent):
        assert _mod._fast_classify_command(msg) == intent

    @pytest.mark.parametrize("msg", [
        "search my notes for zfs",   # needs a slot → not fast
        "how did the proxmox job go",
        "I want to build a thing",
        "",
        "status of the whole world according to me and my dog",
    ])
    def test_non_matches_return_none(self, msg):
        assert _mod._fast_classify_command(msg) is None


# ---------------------------------------------------------------------------
# _nl_command_route — the intercept decision
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestRouteDecision:
    def test_valve_off_short_circuits(self, pipe):
        pipe.valves.nl_command_routing_enabled = False
        with patch.object(pipe, "_classify_command") as clf:
            assert pipe._nl_command_route("what's running", _hist("x")) is None
        clf.assert_not_called()

    def test_fast_path_skips_classifier(self, pipe):
        with patch.object(pipe, "_classify_command") as clf, \
             patch.object(pipe, "_handle_command", return_value="STATUS"):
            gen = pipe._nl_command_route("what's running", _hist("x"))
            out = "".join(gen)
        assert "STATUS" in out
        clf.assert_not_called()          # deterministic tier handled it

    def test_high_confidence_intercepts(self, pipe):
        clf = {"intent": "rag_query", "confidence": "high", "query": "zfs", "job_ref": ""}
        with patch.object(pipe, "_classify_command", return_value=clf), \
             patch.object(pipe, "_handle_command", return_value="RAG") as hc:
            out = "".join(pipe._nl_command_route("look up zfs in my notes", _hist("x")))
        assert "RAG" in out
        hc.assert_called_once_with("/rag zfs", chat_id=None)

    def test_medium_confidence_falls_through(self, pipe):
        clf = {"intent": "status", "confidence": "medium", "query": "", "job_ref": ""}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("maybe show me stuff", _hist("x")) is None

    def test_intent_none_falls_through(self, pipe):
        clf = {"intent": "none", "confidence": "high", "query": "", "job_ref": ""}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("build me a CLI tool", _hist("x")) is None

    @pytest.mark.parametrize("intent", ["rag_query", "jobs_find"])
    def test_missing_required_slot_falls_through(self, pipe, intent):
        # High confidence but empty query → don't intercept into an empty search.
        clf = {"intent": intent, "confidence": "high", "query": "", "job_ref": ""}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("search please", _hist("x")) is None


# ---------------------------------------------------------------------------
# _dispatch_nl_command — intent → canonical slash string
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestDispatchMapping:
    @pytest.mark.parametrize("intent,data,expected", [
        ("status", {}, "/status"),
        ("help", {}, "/help"),
        ("jobs_list", {}, "/jobs list"),
        ("model_list", {}, "/model list"),
        ("model_available", {}, "/model available"),
        ("model_probe", {}, "/model probe"),
        ("rag_query", {"query": "postgres tuning"}, "/rag postgres tuning"),
        ("jobs_find", {"query": "homelab"}, "/jobs find homelab"),
    ])
    def test_slash_translation(self, pipe, intent, data, expected):
        with patch.object(pipe, "_handle_command", return_value="OK") as hc:
            out = "".join(pipe._dispatch_nl_command(intent, data, "raw msg", chat_id="c1"))
        assert out == "OK"
        hc.assert_called_once_with(expected, chat_id="c1")


# ---------------------------------------------------------------------------
# _nl_results / _resolve_job_ref — job-reference resolution
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestResultsResolution:
    def test_no_ref_uses_recall(self, pipe):
        with patch.object(pipe, "_handle_command", return_value="RES") as hc:
            out = "".join(pipe._nl_results({"job_ref": ""}, chat_id="c1"))
        assert out == "RES"
        hc.assert_called_once_with("/results", chat_id="c1")

    def test_unique_match_dispatches_with_id(self, pipe):
        resolved = ({"job_id": "job-42", "title": "proxmox cluster"}, False,
                    [{"job_id": "job-42", "title": "proxmox cluster"}])
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command", return_value="RES") as hc:
            out = "".join(pipe._nl_results({"job_ref": "proxmox"}, chat_id="c1"))
        assert out == "RES"
        hc.assert_called_once_with("/results job-42", chat_id="c1")

    def test_ambiguous_shows_pick_list(self, pipe):
        cands = [{"job_id": "a", "title": "proxmox one"},
                 {"job_id": "b", "title": "proxmox two"}]
        resolved = (cands[0], True, cands)
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe._nl_results({"job_ref": "proxmox"}, chat_id="c1"))
        # Reuses the assist pick-list renderer + hidden ordered-id marker.
        assert "ASSIST_PICK:a,b" in out
        hc.assert_not_called()

    def test_no_match_clarifies(self, pipe):
        with patch.object(pipe, "_resolve_job_ref", return_value=(None, False, [])), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe._nl_results({"job_ref": "nonesuch"}, chat_id="c1"))
        assert "couldn't find a job" in out.lower()
        assert "nonesuch" in out
        hc.assert_not_called()

    def test_resolve_job_ref_normalizes_and_matches(self, pipe):
        body = {"jobs": [
            {"id": "job-1", "title": "proxmox homelab cluster", "status": "completed"},
            {"id": "job-2", "title": "kubernetes ingress", "status": "running"},
        ]}
        with patch.object(_mod._HTTP_SESSION, "get",
                          return_value=_make_response(200, body)):
            match, ambiguous, cands = pipe._resolve_job_ref("proxmox homelab")
        assert match["job_id"] == "job-1"
        assert ambiguous is False
        assert {c["job_id"] for c in cands} == {"job-1", "job-2"}


# ---------------------------------------------------------------------------
# pipe() end-to-end — intercept vs. triage fall-through
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestPipeEndToEnd:
    def _drive(self, pipe, msg, *, clf):
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_classify_command", return_value=clf), \
             patch.object(pipe, "_handle_command", return_value="CMD") as hc:
            out = "".join(pipe.pipe(msg, "model-id", _hist(msg), {}))
        return out, triage, hc

    def test_clear_read_is_intercepted(self, pipe):
        clf = {"intent": "status", "confidence": "high", "query": "", "job_ref": ""}
        out, triage, hc = self._drive(pipe, "how many jobs are going right now", clf=clf)
        assert "CMD" in out and "TRIAGE" not in out
        triage.assert_not_called()
        hc.assert_called_once_with("/status", chat_id=None)

    def test_idea_falls_through_to_triage(self, pipe):
        clf = {"intent": "none", "confidence": "high", "query": "", "job_ref": ""}
        out, triage, hc = self._drive(
            pipe, "I want to build a screenshot-to-PDF tool", clf=clf,
        )
        assert "TRIAGE" in out
        triage.assert_called_once()
        hc.assert_not_called()

    def test_fast_phrase_intercepts_without_classifier(self, pipe):
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_classify_command") as clf, \
             patch.object(pipe, "_handle_command", return_value="CMD"):
            out = "".join(pipe.pipe("list my jobs", "model-id", _hist("list my jobs"), {}))
        assert "CMD" in out
        triage.assert_not_called()
        clf.assert_not_called()

    def test_slash_command_unaffected(self, pipe):
        # A real slash command dispatches earlier and never reaches the NL layer.
        pipe.valves.advanced_commands_enabled = True
        with patch.object(pipe, "_nl_command_route") as nl, \
             patch.object(pipe, "_handle_command", return_value="JOBS"):
            out = "".join(pipe.pipe("/jobs", "model-id", _hist("/jobs"), {}))
        assert "JOBS" in out
        nl.assert_not_called()
