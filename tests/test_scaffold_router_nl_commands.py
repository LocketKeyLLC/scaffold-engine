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
        # §17.655 (Phase 4) — remaining safe reads.
        ("what's scheduled", "schedule_list"),
        ("list my schedules", "schedule_list"),
        ("show my research", "research_list"),
        ("recent research", "research_list"),
        ("health check", "health"),
        ("is everything healthy", "health"),
        ("show my config", "config"),
        ("what am i working on", "work_here"),
        ("what's next", "work_next"),
        ("what should i do next", "work_next"),
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
        # Phase 2 direct (cheap/reversible) writes:
        ("model_reset", {}, "/model reset"),
        ("model_set", {"model_role": "coder", "model_name": "kimi:cloud"},
         "/model set coder kimi:cloud"),
        ("optimize", {"prompt": "write a haiku"}, "/optimize write a haiku"),
        # Phase 4 (§17.655) no-slot reads:
        ("schedule_list", {}, "/schedule list"),
        ("health", {}, "/health"),
        ("config", {}, "/config"),
        ("work_here", {}, "/here"),
        ("work_next", {}, "/next"),
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

    def test_ambiguous_shows_plain_disambiguation_not_assist_pick(self, pipe):
        cands = [{"job_id": "a", "title": "proxmox one", "status": "completed"},
                 {"job_id": "b", "title": "proxmox two", "status": "running"}]
        resolved = (cands[0], True, cands)
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe._nl_results({"job_ref": "proxmox"}, chat_id="c1"))
        # MUST NOT carry the assist pick-list marker — a bare "1" follow-up would
        # otherwise be captured by the assist-start resolver and START a session.
        assert "ASSIST_PICK" not in out
        assert "`a`" in out and "`b`" in out          # ids listed for explicit pick
        assert "/results <id>" in out
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

    def test_resolve_job_ref_is_recency_independent(self, pipe):
        # §17.636 — a job NOT in the recent list is still found via a
        # server-side ?q=<distinctive-token> title search (the "could not find
        # the isolated VM job" bug, where test jobs pushed it past the recent
        # window). Noise words ("job") are dropped from the search token.
        def fake_fetch(params):
            if params.get("q") == "isolated":
                return [{"id": "j-iso", "title": "Isolated VM Setup for Media",
                         "status": "awaiting_assist"}]
            return []  # not in the recent/other-token results
        with patch.object(pipe, "_fetch_jobs", side_effect=fake_fetch):
            match, ambiguous, cands = pipe._resolve_job_ref("isolated VM job")
        assert match["job_id"] == "j-iso"   # found despite not being "recent"
        assert ambiguous is False           # single search hit → unambiguous

    def test_resolve_job_ref_no_match_returns_none(self, pipe):
        with patch.object(pipe, "_fetch_jobs", return_value=[]):
            match, ambiguous, cands = pipe._resolve_job_ref("nonexistent zzz")
        assert match is None and cands == []


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


# ===========================================================================
# §17.629 — Phase 2: mutating / expensive intents
# ===========================================================================


@pytest.mark.smoke
class TestPhase2RequiredSlots:
    """A write intent missing a required slot falls through to triage rather
    than firing an empty/half action."""
    @pytest.mark.parametrize("intent,data", [
        ("research_topic", {"confidence": "high"}),                       # no topic
        ("schedule_add", {"confidence": "high", "topic": "x"}),           # no cron
        ("schedule_add", {"confidence": "high", "cron": "0 9 * * 1"}),    # no topic
        ("model_set", {"confidence": "high", "model_role": "coder"}),     # no name
        ("model_set", {"confidence": "high", "model_name": "kimi"}),      # no role
        ("optimize", {"confidence": "high"}),                            # no prompt
        ("jobs_rename", {"confidence": "high", "job_ref": "x"}),          # no new_name
        ("jobs_rename", {"confidence": "high", "new_name": "Y"}),         # no job_ref
    ])
    def test_missing_slot_falls_through(self, pipe, intent, data):
        clf = {"intent": intent, **data}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("do a thing", _hist("x")) is None

    def test_full_slots_intercept(self, pipe):
        clf = {"intent": "research_topic", "confidence": "high",
               "topic": "postgres tuning", "depth": "deep"}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("research postgres", _hist("x")) is not None


@pytest.mark.smoke
class TestJobsRename:
    def test_unique_match_renames(self, pipe):
        resolved = ({"job_id": "job-9", "title": "old"}, False,
                    [{"job_id": "job-9", "title": "old"}])
        data = {"job_ref": "old", "new_name": "New Title"}
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command", return_value="RENAMED") as hc:
            out = "".join(pipe._nl_rename(data, chat_id="c1"))
        assert out == "RENAMED"
        hc.assert_called_once_with("/jobs rename job-9 New Title", chat_id="c1")

    def test_ambiguous_lists_without_marker(self, pipe):
        cands = [{"job_id": "a", "title": "lab one", "status": "completed"},
                 {"job_id": "b", "title": "lab two", "status": "completed"}]
        with patch.object(pipe, "_resolve_job_ref", return_value=(cands[0], True, cands)), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe._nl_rename({"job_ref": "lab", "new_name": "X"}, chat_id="c1"))
        assert "ASSIST_PICK" not in out
        assert "/jobs rename <id> X" in out           # new title preserved in hint
        hc.assert_not_called()

    def test_no_match_clarifies(self, pipe):
        with patch.object(pipe, "_resolve_job_ref", return_value=(None, False, [])), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe._nl_rename({"job_ref": "nope", "new_name": "X"}, chat_id="c1"))
        assert "couldn't find a job" in out.lower()
        hc.assert_not_called()


@pytest.mark.smoke
class TestConfirmCards:
    """Expensive writes render a confirm card and do NOT fire directly."""
    def test_research_renders_confirm_not_launch(self, pipe):
        data = {"topic": "zfs tuning", "depth": "deep"}
        with patch.object(pipe, "_handle_research") as launch:
            out = "".join(pipe._dispatch_nl_command("research_topic", data, "raw"))
        assert "NL_CONFIRM:" in out          # hidden action marker present
        assert "zfs tuning" in out
        assert "deep" in out
        launch.assert_not_called()           # nothing runs until confirmed

    def test_schedule_renders_confirm_not_create(self, pipe):
        data = {"topic": "ai news", "cron": "0 9 * * 1", "tz": "UTC", "depth": "medium"}
        with patch.object(pipe, "_handle_schedule") as create:
            out = "".join(pipe._dispatch_nl_command("schedule_add", data, "raw"))
        assert "NL_CONFIRM:" in out
        assert "0 9 * * 1" in out
        create.assert_not_called()


@pytest.mark.smoke
class TestConfirmFollowup:
    def _confirm_msg(self, pipe, intent, slots):
        card = pipe._render_nl_confirm(intent, slots, "summary")
        return [
            {"role": "user", "content": "research zfs deeply"},
            {"role": "assistant", "content": card},
            {"role": "user", "content": "go"},
        ]

    def test_extract_recovers_pending_action(self, pipe):
        msgs = self._confirm_msg(pipe, "research_topic", {"topic": "zfs", "depth": "deep"})
        pend = pipe._extract_pending_nl_confirm(msgs)
        assert pend["intent"] == "research_topic"
        assert pend["slots"]["topic"] == "zfs"

    def test_no_marker_returns_none(self, pipe):
        msgs = [{"role": "assistant", "content": "just a normal reply"},
                {"role": "user", "content": "go"}]
        assert pipe._extract_pending_nl_confirm(msgs) is None

    def test_only_most_recent_assistant_turn_counts(self, pipe):
        card = pipe._render_nl_confirm("research_topic", {"topic": "old"}, "s")
        msgs = [
            {"role": "assistant", "content": card},         # stale confirm
            {"role": "user", "content": "never mind"},
            {"role": "assistant", "content": "ok, what else?"},  # newest, no marker
            {"role": "user", "content": "go"},
        ]
        assert pipe._extract_pending_nl_confirm(msgs) is None

    @pytest.mark.parametrize("word,ok", [
        ("go", True), ("yes", True), ("Yes!", True), ("run it", True),
        ("go ahead", True), ("no", False), ("make it shallow", False),
        ("wait", False), ("", False),
    ])
    def test_affirmative_detection(self, pipe, word, ok):
        assert pipe._is_affirmative(word) is ok

    @pytest.mark.parametrize("word,ok", [
        ("no", True), ("no, cancel that", True), ("never mind", True),
        ("nevermind", True), ("not now", True), ("cancel", True),
        ("don't", True), ("stop", True),
        ("yes", False), ("go", False), ("make it deep instead", False), ("", False),
    ])
    def test_negative_detection(self, pipe, word, ok):
        assert pipe._is_negative(word) is ok

    def test_negative_reply_cancels_cleanly_no_triage(self, pipe):
        # §17.637 — declining a pending confirm renders a clean cancellation and
        # STOPS (previously it fell through to the planner: the reported
        # "reverted to Scope/Options/Gaps" after saying no to a delete).
        card = pipe._render_nl_confirm(
            "jobs_delete", {"id": "j-1", "label": "Isolated VM Setup", "noun": "job"}, "⚠️")
        hist = [
            {"role": "user", "content": "delete the isolated VM job"},
            {"role": "assistant", "content": card},
            {"role": "user", "content": "no, cancel that"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_execute_nl_action") as ex:
            out = "".join(pipe.pipe("no, cancel that", "m", hist, {}))
        assert "Cancelled" in out and "not deleted" in out
        assert "Isolated VM Setup" in out
        assert "TRIAGE" not in out
        triage.assert_not_called()
        ex.assert_not_called()          # nothing executed

    def test_execute_research_fires_handle_research(self, pipe):
        pend = {"intent": "research_topic", "slots": {"topic": "zfs", "depth": "deep"}}
        with patch.object(pipe, "_handle_research",
                          side_effect=lambda cmd: iter([f"RUN::{cmd}"])) as hr:
            out = "".join(pipe._execute_nl_action(pend))
        assert "RUN::/research zfs --depth=deep" in out
        hr.assert_called_once()

    def test_execute_schedule_fires_handle_schedule(self, pipe):
        pend = {"intent": "schedule_add",
                "slots": {"topic": "ai news", "cron": "0 9 * * 1", "tz": "UTC", "depth": "medium"}}
        with patch.object(pipe, "_handle_schedule", return_value="SCHEDULED") as hs:
            out = "".join(pipe._execute_nl_action(pend))
        assert out == "SCHEDULED"
        cmd = hs.call_args[0][0]
        assert cmd.startswith('/schedule add "0 9 * * 1"')
        assert "--depth=medium" in cmd and "ai news" in cmd


@pytest.mark.smoke
class TestPipeConfirmFlow:
    def test_research_nl_shows_confirm_then_affirmative_launches(self, pipe):
        # Turn 1: NL research request → confirm card, nothing launched.
        clf = {"intent": "research_topic", "confidence": "high",
               "topic": "zfs tuning", "depth": "deep"}
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE"), \
             patch.object(pipe, "_classify_command", return_value=clf), \
             patch.object(pipe, "_handle_research") as launch:
            card = "".join(pipe.pipe("research zfs tuning deeply",
                                     "m", _hist("research zfs tuning deeply"), {}))
        assert "NL_CONFIRM:" in card
        launch.assert_not_called()

        # Turn 2: the card is in history + user says "go" → launch fires.
        history = [
            {"role": "user", "content": "research zfs tuning deeply"},
            {"role": "assistant", "content": card},
            {"role": "user", "content": "go"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_classify_command") as clf2, \
             patch.object(pipe, "_handle_research",
                          side_effect=lambda cmd: iter([f"RUN::{cmd}"])) as launch2:
            out = "".join(pipe.pipe("go", "m", history, {}))
        assert "RUN::/research zfs tuning --depth=deep" in out
        triage.assert_not_called()
        clf2.assert_not_called()             # affirmative short-circuits classification

    def test_non_affirmative_after_confirm_falls_through(self, pipe):
        card = pipe._render_nl_confirm("research_topic", {"topic": "zfs", "depth": "deep"}, "s")
        history = [
            {"role": "user", "content": "research zfs"},
            {"role": "assistant", "content": card},
            {"role": "user", "content": "actually never mind, tell me a joke"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_classify_command",
                          return_value={"intent": "none", "confidence": "low"}), \
             patch.object(pipe, "_handle_research") as launch:
            out = "".join(pipe.pipe("actually never mind, tell me a joke", "m", history, {}))
        assert "TRIAGE" in out
        launch.assert_not_called()           # confirm discarded, nothing ran


# ===========================================================================
# §17.803 — role→model swap proposals (staged by the learning job)
# ===========================================================================


@pytest.mark.smoke
class TestModelProposals:
    _PROP = {
        "id": 7, "role": "model_coder", "task": "codegen",
        "incumbent_model": "inc:cloud", "candidate_model": "cand:cloud",
        "incumbent_rate": 0.8, "candidate_rate": 1.0, "speedup": 2.0,
    }

    def test_proposals_list_read_only(self, pipe):
        body = {"proposals": [self._PROP], "count": 1}
        with patch.object(_mod._HTTP_SESSION, "get",
                          return_value=_make_response(200, body)):
            out = pipe._handle_model("/model proposals")
        assert "cand:cloud" in out and "coder" in out
        assert "NL_CONFIRM:" not in out            # listing never carries a marker

    def test_proposals_empty(self, pipe):
        with patch.object(_mod._HTTP_SESSION, "get",
                          return_value=_make_response(200, {"proposals": [], "count": 0})):
            out = pipe._handle_model("/model proposals")
        assert "No open model-role proposals" in out

    def test_apply_renders_confirm_card(self, pipe):
        with patch.object(_mod._HTTP_SESSION, "get",
                          return_value=_make_response(200, {"proposals": [self._PROP]})):
            card = pipe._handle_model("/model apply coder")
        assert "NL_CONFIRM:" in card               # a confirm card, not an apply
        pend = pipe._extract_pending_nl_confirm(
            [{"role": "assistant", "content": card}, {"role": "user", "content": "go"}])
        assert pend["intent"] == "model_proposal_apply"
        assert pend["slots"] == {"id": 7, "role": "model_coder", "candidate": "cand:cloud"}

    def test_apply_unknown_role_no_card(self, pipe):
        with patch.object(_mod._HTTP_SESSION, "get",
                          return_value=_make_response(200, {"proposals": [self._PROP]})):
            out = pipe._handle_model("/model apply verifier")
        assert "No open proposal" in out and "NL_CONFIRM:" not in out

    def test_execute_apply_posts_accept(self, pipe):
        pend = {"intent": "model_proposal_apply",
                "slots": {"id": 7, "role": "model_coder", "candidate": "cand:cloud"}}
        body = {"id": 7, "role": "model_coder", "model": "cand:cloud", "applied": True}
        with patch.object(_mod._HTTP_SESSION, "post",
                          return_value=_make_response(200, body)) as post:
            out = "".join(pipe._execute_nl_action(pend))
        assert "Applied" in out and "cand:cloud" in out
        assert post.call_args[0][0].endswith("/models/proposals/7/accept")

    def test_execute_apply_stale_404(self, pipe):
        pend = {"intent": "model_proposal_apply",
                "slots": {"id": 7, "role": "model_coder", "candidate": "cand:cloud"}}
        with patch.object(_mod._HTTP_SESSION, "post",
                          return_value=_make_response(404, {"detail": "gone"})):
            out = "".join(pipe._execute_nl_action(pend))
        assert "no longer open" in out

    def test_cancelled_message(self, pipe):
        msg = pipe._render_confirm_cancelled({"intent": "model_proposal_apply", "slots": {}})
        assert "no model was swapped" in msg


# ===========================================================================
# §17.630 — Phase 3: destructive intents (always confirmed)
# ===========================================================================


@pytest.mark.smoke
class TestPhase3RequiredSlots:
    @pytest.mark.parametrize("intent", ["jobs_delete", "schedule_delete", "research_delete"])
    def test_missing_target_falls_through(self, pipe, intent):
        clf = {"intent": intent, "confidence": "high"}   # no target_ref
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("delete something", _hist("x")) is None


@pytest.mark.smoke
class TestNlDelete:
    """A delete resolves the target and renders a confirm card — nothing is
    removed in _dispatch (only _execute_nl_action deletes)."""
    @pytest.mark.parametrize("intent,resolver,noun", [
        ("jobs_delete", "_resolve_job_ref", "job"),
        ("schedule_delete", "_resolve_schedule_ref", "schedule"),
        ("research_delete", "_resolve_research_ref", "research session"),
    ])
    def test_unique_match_renders_confirm(self, pipe, intent, resolver, noun):
        resolved = ({"job_id": "id-7", "title": "kubernetes news"}, False,
                    [{"job_id": "id-7", "title": "kubernetes news"}])
        with patch.object(pipe, resolver, return_value=resolved), \
             patch.object(pipe, "_handle_command") as hc, \
             patch.object(pipe, "_handle_schedule") as hs, \
             patch.object(pipe, "_handle_research_mgmt") as hr:
            out = "".join(pipe._dispatch_nl_command(intent, {"target_ref": "kube"}, "raw"))
        assert "NL_CONFIRM:" in out              # gated behind confirm
        assert "Permanently delete" in out
        assert noun in out
        assert "id-7" in out
        # NOTHING fired yet:
        hc.assert_not_called(); hs.assert_not_called(); hr.assert_not_called()

    def test_ambiguous_lists_without_marker(self, pipe):
        cands = [{"job_id": "a", "title": "kube one", "status": "running"},
                 {"job_id": "b", "title": "kube two", "status": "running"}]
        with patch.object(pipe, "_resolve_job_ref", return_value=(cands[0], True, cands)):
            out = "".join(pipe._nl_delete("jobs_delete", {"target_ref": "kube"}))
        assert "ASSIST_PICK" not in out and "NL_CONFIRM" not in out
        assert "/jobs delete <id>" in out

    def test_no_match_clarifies(self, pipe):
        with patch.object(pipe, "_resolve_schedule_ref", return_value=(None, False, [])):
            out = "".join(pipe._nl_delete("schedule_delete", {"target_ref": "nope"}))
        assert "couldn't find a schedule" in out.lower()
        assert "NL_CONFIRM" not in out


@pytest.mark.smoke
class TestDeleteResolvers:
    def test_schedule_ref_normalizes(self, pipe):
        body = {"schedules": [
            {"id": 5, "topic": "kubernetes security news", "cron_expression": "0 9 * * 1"},
            {"id": 6, "topic": "postgres releases"},
        ]}
        with patch.object(_mod._HTTP_SESSION, "get", return_value=_make_response(200, body)):
            match, ambiguous, cands = pipe._resolve_schedule_ref("kubernetes security")
        assert match["job_id"] == "5"          # id stringified
        assert ambiguous is False
        assert {c["job_id"] for c in cands} == {"5", "6"}

    def test_research_ref_normalizes(self, pipe):
        body = {"sessions": [
            {"id": "sess-1", "topic": "zfs on non-ecc ram"},
            {"id": "sess-2", "topic": "proxmox clustering guide"},
        ]}
        with patch.object(_mod._HTTP_SESSION, "get", return_value=_make_response(200, body)):
            match, ambiguous, cands = pipe._resolve_research_ref("proxmox clustering")
        assert match["job_id"] == "sess-2"
        assert ambiguous is False


@pytest.mark.smoke
class TestExecuteDelete:
    """Only after an affirmative does _execute_nl_action fire the underlying
    delete — and it must carry the handler's own confirm token."""
    def test_jobs_delete_uses_confirm_token(self, pipe):
        pend = {"intent": "jobs_delete", "slots": {"id": "job-3", "label": "old"}}
        with patch.object(pipe, "_handle_command", return_value="DELETED") as hc:
            out = "".join(pipe._execute_nl_action(pend, chat_id="c1"))
        assert out == "DELETED"
        hc.assert_called_once_with("/jobs delete job-3 confirm", chat_id="c1")

    def test_schedule_delete_dispatches(self, pipe):
        pend = {"intent": "schedule_delete", "slots": {"id": "9"}}
        with patch.object(pipe, "_handle_schedule", return_value="GONE") as hs:
            out = "".join(pipe._execute_nl_action(pend))
        assert out == "GONE"
        hs.assert_called_once_with("/schedule delete 9")

    def test_research_delete_uses_confirm_token(self, pipe):
        pend = {"intent": "research_delete", "slots": {"id": "sess-1"}}
        with patch.object(pipe, "_handle_research_mgmt",
                          side_effect=lambda cmd: iter([f"R::{cmd}"])) as hr:
            out = "".join(pipe._execute_nl_action(pend))
        assert "R::/research/delete sess-1 confirm" in out
        hr.assert_called_once()


@pytest.mark.smoke
class TestPipeDeleteFlow:
    def test_delete_shows_confirm_then_affirmative_removes(self, pipe):
        clf = {"intent": "jobs_delete", "confidence": "high", "target_ref": "kube"}
        resolved = ({"job_id": "job-3", "title": "kubernetes job"}, False,
                    [{"job_id": "job-3", "title": "kubernetes job"}])
        # Turn 1: NL delete request → confirm card, nothing removed.
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE"), \
             patch.object(pipe, "_classify_command", return_value=clf), \
             patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command") as hc:
            card = "".join(pipe.pipe("delete the kubernetes job", "m",
                                     _hist("delete the kubernetes job"), {}))
        assert "NL_CONFIRM:" in card and "Permanently delete" in card
        hc.assert_not_called()

        # Turn 2: card in history + "yes" → the delete fires with confirm token.
        history = [
            {"role": "user", "content": "delete the kubernetes job"},
            {"role": "assistant", "content": card},
            {"role": "user", "content": "yes"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_classify_command") as clf2, \
             patch.object(pipe, "_handle_command", return_value="DELETED") as hc2:
            out = "".join(pipe.pipe("yes", "m", history, {}))
        assert "DELETED" in out
        triage.assert_not_called()
        clf2.assert_not_called()
        hc2.assert_called_once_with("/jobs delete job-3 confirm", chat_id=None)

    def test_delete_confirm_then_no_does_not_remove(self, pipe):
        card = pipe._render_nl_confirm(
            "jobs_delete", {"id": "job-3", "label": "kube"}, "⚠️ Permanently delete?")
        history = [
            {"role": "user", "content": "delete the kube job"},
            {"role": "assistant", "content": card},
            {"role": "user", "content": "no wait, cancel that"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE"), \
             patch.object(pipe, "_classify_command",
                          return_value={"intent": "none", "confidence": "low"}), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe.pipe("no wait, cancel that", "m", history, {}))
        # The delete must NOT fire on a non-affirmative reply.
        assert not any("delete" in str(c).lower() for c in
                       [call.args[0] for call in hc.call_args_list])


# ===========================================================================
# §17.655 — Phase 4: remaining safe reads
# ===========================================================================


@pytest.mark.smoke
class TestPhase4Reads:
    def test_research_find_missing_query_falls_through(self, pipe):
        # High confidence but empty query → don't intercept into an empty search.
        clf = {"intent": "research_find", "confidence": "high", "query": ""}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("find my research", _hist("x")) is None

    def test_research_list_dispatches_to_mgmt(self, pipe):
        with patch.object(pipe, "_handle_research_mgmt",
                          side_effect=lambda cmd: iter([f"R::{cmd}"])) as hr:
            out = "".join(pipe._dispatch_nl_command("research_list", {}, "raw"))
        assert "R::/research/list" in out
        hr.assert_called_once()

    def test_research_find_dispatches_query_to_mgmt(self, pipe):
        with patch.object(pipe, "_handle_research_mgmt",
                          side_effect=lambda cmd: iter([f"R::{cmd}"])) as hr:
            out = "".join(pipe._dispatch_nl_command(
                "research_find", {"query": "proxmox"}, "raw"))
        assert "R::/research/find proxmox" in out
        hr.assert_called_once()

    @pytest.mark.parametrize("intent,slash,verb", [
        ("logs", "/logs", "see logs for"),
        ("cost", "/cost", "see the cost of"),
    ])
    def test_logs_cost_no_ref_uses_recall(self, pipe, intent, slash, verb):
        with patch.object(pipe, "_handle_command", return_value="OUT") as hc:
            out = "".join(pipe._dispatch_nl_command(intent, {"job_ref": ""},
                                                    "raw", chat_id="c1"))
        assert out == "OUT"
        hc.assert_called_once_with(slash, chat_id="c1")

    @pytest.mark.parametrize("intent,slash", [("logs", "/logs"), ("cost", "/cost")])
    def test_logs_cost_unique_match_dispatches_with_id(self, pipe, intent, slash):
        resolved = ({"job_id": "job-42", "title": "proxmox"}, False,
                    [{"job_id": "job-42", "title": "proxmox"}])
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command", return_value="OUT") as hc:
            out = "".join(pipe._dispatch_nl_command(intent, {"job_ref": "proxmox"},
                                                    "raw", chat_id="c1"))
        assert out == "OUT"
        hc.assert_called_once_with(f"{slash} job-42", chat_id="c1")

    def test_logs_ambiguous_lists_without_assist_marker(self, pipe):
        cands = [{"job_id": "a", "title": "prox one", "status": "completed"},
                 {"job_id": "b", "title": "prox two", "status": "running"}]
        with patch.object(pipe, "_resolve_job_ref", return_value=(cands[0], True, cands)), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe._dispatch_nl_command("logs", {"job_ref": "prox"}, "raw"))
        assert "ASSIST_PICK" not in out
        assert "/logs <id>" in out
        hc.assert_not_called()

    def test_cost_no_match_clarifies(self, pipe):
        with patch.object(pipe, "_resolve_job_ref", return_value=(None, False, [])), \
             patch.object(pipe, "_handle_command") as hc:
            out = "".join(pipe._dispatch_nl_command("cost", {"job_ref": "nope"}, "raw"))
        assert "couldn't find a job" in out.lower()
        hc.assert_not_called()

    def test_fast_phrase_read_intercepts_end_to_end(self, pipe):
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_classify_command") as clf, \
             patch.object(pipe, "_handle_command", return_value="SCHED"):
            out = "".join(pipe.pipe("list my schedules", "m",
                                    _hist("list my schedules"), {}))
        assert "SCHED" in out
        triage.assert_not_called()
        clf.assert_not_called()          # deterministic tier handled it


# ===========================================================================
# §17.656 — Phase 5: research ingest variants + session rename
# ===========================================================================


@pytest.mark.smoke
class TestPhase5RequiredSlots:
    @pytest.mark.parametrize("intent,data", [
        ("research_url", {"confidence": "high"}),                      # no url
        ("research_github", {"confidence": "high"}),                   # no repo
        ("research_openapi", {"confidence": "high"}),                  # no url
        ("research_rename", {"confidence": "high", "job_ref": "x"}),   # no new_name
        ("research_rename", {"confidence": "high", "new_name": "Y"}),  # no ref
    ])
    def test_missing_slot_falls_through(self, pipe, intent, data):
        clf = {"intent": intent, **data}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("ingest a thing", _hist("x")) is None


@pytest.mark.smoke
class TestIngestConfirmCards:
    """Ingest fetches an external source + writes to the KB → confirm card,
    nothing fetched until an affirmative follow-up."""
    def test_url_renders_confirm_not_launch(self, pipe):
        with patch.object(pipe, "_handle_research") as launch:
            out = "".join(pipe._dispatch_nl_command(
                "research_url", {"url": "https://example.com/post"}, "raw"))
        assert "NL_CONFIRM:" in out
        assert "https://example.com/post" in out
        launch.assert_not_called()

    def test_github_strips_prefix_in_source(self, pipe):
        with patch.object(pipe, "_handle_research"):
            out = "".join(pipe._dispatch_nl_command(
                "research_github", {"repo": "https://github.com/owner/repo"}, "raw"))
        assert "NL_CONFIRM:" in out
        assert "github:owner/repo" in out          # prefix stripped + normalized

    def test_openapi_prefixes_source(self, pipe):
        with patch.object(pipe, "_handle_research"):
            out = "".join(pipe._dispatch_nl_command(
                "research_openapi", {"url": "https://api.x/openapi.json"}, "raw"))
        assert "openapi:https://api.x/openapi.json" in out


@pytest.mark.smoke
class TestIngestExecute:
    @pytest.mark.parametrize("intent,slots,expected", [
        ("research_url", {"source": "https://x/p"}, "/research https://x/p --confirm"),
        ("research_github", {"source": "github:o/r"}, "/research github:o/r --confirm"),
        ("research_openapi", {"source": "openapi:https://x/o.json"},
         "/research openapi:https://x/o.json --confirm"),
    ])
    def test_execute_fires_handle_research(self, pipe, intent, slots, expected):
        pend = {"intent": intent, "slots": slots}
        with patch.object(pipe, "_handle_research",
                          side_effect=lambda cmd: iter([f"RUN::{cmd}"])) as hr:
            out = "".join(pipe._execute_nl_action(pend))
        assert f"RUN::{expected}" in out
        hr.assert_called_once()

    def test_ingest_cancel_message(self, pipe):
        msg = pipe._render_confirm_cancelled(
            {"intent": "research_url", "slots": {"source": "https://x"}})
        assert "ingested" in msg.lower()


@pytest.mark.smoke
class TestResearchRename:
    def test_unique_match_renames_directly(self, pipe):
        resolved = ({"job_id": "sess-2", "title": "zfs"}, False,
                    [{"job_id": "sess-2", "title": "zfs"}])
        with patch.object(pipe, "_resolve_research_ref", return_value=resolved), \
             patch.object(pipe, "_handle_research_mgmt",
                          side_effect=lambda cmd: iter([f"R::{cmd}"])) as hr:
            out = "".join(pipe._nl_research_rename(
                {"job_ref": "zfs", "new_name": "ZFS Tuning Notes"}))
        assert "R::/research/rename sess-2 ZFS Tuning Notes" in out
        hr.assert_called_once()

    def test_ambiguous_lists_without_marker(self, pipe):
        cands = [{"job_id": "a", "title": "zfs one", "status": "completed"},
                 {"job_id": "b", "title": "zfs two", "status": "completed"}]
        with patch.object(pipe, "_resolve_research_ref", return_value=(cands[0], True, cands)), \
             patch.object(pipe, "_handle_research_mgmt") as hr:
            out = "".join(pipe._nl_research_rename({"job_ref": "zfs", "new_name": "X"}))
        assert "ASSIST_PICK" not in out
        assert "/research/rename <id> X" in out
        hr.assert_not_called()

    def test_no_match_clarifies(self, pipe):
        with patch.object(pipe, "_resolve_research_ref", return_value=(None, False, [])):
            out = "".join(pipe._nl_research_rename({"job_ref": "nope", "new_name": "X"}))
        assert "couldn't find a research session" in out.lower()

    def test_dispatch_routes_rename(self, pipe):
        with patch.object(pipe, "_nl_research_rename",
                          side_effect=lambda data: iter(["RENAMED"])) as nr:
            out = "".join(pipe._dispatch_nl_command(
                "research_rename", {"job_ref": "zfs", "new_name": "X"}, "raw"))
        assert "RENAMED" in out
        nr.assert_called_once()


# ===========================================================================
# §17.657 — Phase 6: workflow control (all confirmed)
# ===========================================================================


@pytest.mark.smoke
class TestPhase6RequiredSlots:
    @pytest.mark.parametrize("intent,data", [
        ("confirm_job", {"confidence": "high"}),                          # no job_ref
        ("execute_job", {"confidence": "high"}),                          # no job_ref
        ("cancel_job", {"confidence": "high"}),                           # no job_ref
        # §17.659 — skip/retry need only job_ref now (node auto-resolves); a
        # bare node with no job still falls through.
        ("skip_node", {"confidence": "high", "node_key": "T3"}),          # no job_ref
        ("retry_node", {"confidence": "high", "node_key": "T3"}),         # no job_ref
    ])
    def test_missing_slot_falls_through(self, pipe, intent, data):
        clf = {"intent": intent, **data}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("do a thing", _hist("x")) is None

    @pytest.mark.parametrize("intent", ["skip_node", "retry_node"])
    def test_node_verb_intercepts_on_job_ref_alone(self, pipe, intent):
        # §17.659 — node_key optional: job_ref alone is enough to intercept.
        clf = {"intent": intent, "confidence": "high", "job_ref": "kube"}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("retry the failed step", _hist("x")) is not None

    def test_cleanup_needs_no_slot(self, pipe):
        clf = {"intent": "cleanup", "confidence": "high"}
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("clean up stale jobs", _hist("x")) is not None


@pytest.mark.smoke
class TestWorkflowConfirmCards:
    """Every workflow verb renders a confirm card and fires NOTHING until an
    affirmative follow-up."""
    @pytest.mark.parametrize("intent,data", [
        ("confirm_job", {"job_ref": "proxmox"}),
        ("execute_job", {"job_ref": "proxmox"}),
        ("cancel_job", {"job_ref": "proxmox"}),
        ("skip_node", {"job_ref": "proxmox", "node_key": "T3"}),
        ("retry_node", {"job_ref": "proxmox", "node_key": "T3"}),
    ])
    def test_unique_match_renders_confirm_not_fire(self, pipe, intent, data):
        resolved = ({"job_id": "job-7", "title": "proxmox cluster",
                     "status": "awaiting_confirmation"}, False,
                    [{"job_id": "job-7", "title": "proxmox cluster"}])
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command") as hc, \
             patch.object(pipe, "_handle_confirm") as hcf, \
             patch.object(pipe, "_handle_execute") as hex_:
            out = "".join(pipe._dispatch_nl_command(intent, data, "raw", chat_id="c1"))
        assert "NL_CONFIRM:" in out
        assert "job-7" in out
        hc.assert_not_called(); hcf.assert_not_called(); hex_.assert_not_called()

    def test_node_key_shown_in_card(self, pipe):
        resolved = ({"job_id": "job-7", "title": "kube", "status": "failed"}, False,
                    [{"job_id": "job-7", "title": "kube"}])
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved):
            out = "".join(pipe._dispatch_nl_command(
                "skip_node", {"job_ref": "kube", "node_key": "T3"}, "raw"))
        assert "`T3`" in out


@pytest.mark.smoke
class TestNodeAutoResolve:
    """§17.659 — skip/retry with no named node_key auto-resolve the failed one."""
    def _job(self):
        return ({"job_id": "job-7", "title": "kube", "status": "failed"}, False,
                [{"job_id": "job-7", "title": "kube"}])

    @pytest.mark.parametrize("intent", ["skip_node", "retry_node"])
    def test_single_failing_node_auto_used(self, pipe, intent):
        with patch.object(pipe, "_resolve_job_ref", return_value=self._job()), \
             patch.object(pipe, "_resolve_failing_nodes",
                          return_value=[{"node_key": "T4", "status": "failed"}]):
            out = "".join(pipe._dispatch_nl_command(intent, {"job_ref": "kube"}, "raw"))
        assert "NL_CONFIRM:" in out and "`T4`" in out       # auto-picked node in card

    @pytest.mark.parametrize("intent", ["skip_node", "retry_node"])
    def test_multiple_failing_nodes_lists(self, pipe, intent):
        with patch.object(pipe, "_resolve_job_ref", return_value=self._job()), \
             patch.object(pipe, "_resolve_failing_nodes",
                          return_value=[{"node_key": "T2", "failure_reason": "bad"},
                                        {"node_key": "T5", "failure_reason": "worse"}]):
            out = "".join(pipe._dispatch_nl_command(intent, {"job_ref": "kube"}, "raw"))
        assert "NL_CONFIRM" not in out                       # not auto-fired
        assert "`T2`" in out and "`T5`" in out
        assert "which one" in out.lower()

    @pytest.mark.parametrize("intent,word", [("skip_node", "skip"), ("retry_node", "retry")])
    def test_no_failing_nodes_says_so(self, pipe, intent, word):
        with patch.object(pipe, "_resolve_job_ref", return_value=self._job()), \
             patch.object(pipe, "_resolve_failing_nodes", return_value=[]):
            out = "".join(pipe._dispatch_nl_command(intent, {"job_ref": "kube"}, "raw"))
        assert "NL_CONFIRM" not in out
        assert word in out.lower() and "no failed" in out.lower()

    def test_named_node_key_skips_autoresolve(self, pipe):
        with patch.object(pipe, "_resolve_job_ref", return_value=self._job()), \
             patch.object(pipe, "_resolve_failing_nodes") as rf:
            out = "".join(pipe._dispatch_nl_command(
                "retry_node", {"job_ref": "kube", "node_key": "T9"}, "raw"))
        assert "NL_CONFIRM:" in out and "`T9`" in out
        rf.assert_not_called()                              # no lookup when named

    def test_resolve_failing_nodes_filters(self, pipe):
        body = {"nodes": [
            {"node_key": "T1", "status": "done"},
            {"node_key": "T2", "status": "failed"},
            {"node_key": "T3", "status": "blocked"},
            {"node_key": "T4", "status": "running"}]}
        with patch.object(_mod._HTTP_SESSION, "get",
                          return_value=_make_response(200, body)):
            out = pipe._resolve_failing_nodes("job-7")
        assert {n["node_key"] for n in out} == {"T2", "T3"}

    def test_ambiguous_lists_without_marker(self, pipe):
        cands = [{"job_id": "a", "title": "prox one", "status": "planning"},
                 {"job_id": "b", "title": "prox two", "status": "planning"}]
        with patch.object(pipe, "_resolve_job_ref", return_value=(cands[0], True, cands)):
            out = "".join(pipe._dispatch_nl_command(
                "execute_job", {"job_ref": "prox"}, "raw"))
        assert "ASSIST_PICK" not in out and "NL_CONFIRM" not in out
        assert "/execute <id>" in out

    def test_no_match_clarifies(self, pipe):
        with patch.object(pipe, "_resolve_job_ref", return_value=(None, False, [])):
            out = "".join(pipe._dispatch_nl_command(
                "cancel_job", {"job_ref": "nope"}, "raw"))
        assert "couldn't find a job" in out.lower()
        assert "NL_CONFIRM" not in out

    def test_cleanup_confirm_card(self, pipe):
        out = "".join(pipe._dispatch_nl_command("cleanup", {}, "raw"))
        assert "NL_CONFIRM:" in out
        assert "reaper" in out.lower()


@pytest.mark.smoke
class TestWorkflowExecute:
    """After an affirmative, _execute_nl_action fires the underlying command."""
    def test_confirm_streams_via_handle_confirm(self, pipe):
        pend = {"intent": "confirm_job", "slots": {"id": "job-7", "label": "prox"}}
        with patch.object(pipe, "_handle_confirm",
                          side_effect=lambda cmd: iter([f"C::{cmd}"])) as h:
            out = "".join(pipe._execute_nl_action(pend, chat_id="c1"))
        assert "C::/confirm job-7" in out
        h.assert_called_once()

    def test_execute_streams_via_handle_execute(self, pipe):
        pend = {"intent": "execute_job", "slots": {"id": "job-7"}}
        with patch.object(pipe, "_handle_execute",
                          side_effect=lambda cmd, **k: iter([f"E::{cmd}"])) as h:
            out = "".join(pipe._execute_nl_action(pend, chat_id="c1"))
        assert "E::/execute job-7" in out
        h.assert_called_once_with("/execute job-7", chat_id="c1")

    def test_cancel_dispatches_command(self, pipe):
        pend = {"intent": "cancel_job", "slots": {"id": "job-7"}}
        with patch.object(pipe, "_handle_command", return_value="CANCELLED") as hc:
            out = "".join(pipe._execute_nl_action(pend, chat_id="c1"))
        assert out == "CANCELLED"
        hc.assert_called_once_with("/cancel job-7", chat_id="c1")

    def test_skip_dispatches_command(self, pipe):
        pend = {"intent": "skip_node", "slots": {"id": "job-7", "node_key": "T3"}}
        with patch.object(pipe, "_handle_command", return_value="SKIPPED") as hc:
            out = "".join(pipe._execute_nl_action(pend, chat_id="c1"))
        hc.assert_called_once_with("/skip job-7 T3", chat_id="c1")

    def test_retry_dispatches_command(self, pipe):
        pend = {"intent": "retry_node", "slots": {"id": "job-7", "node_key": "T3"}}
        with patch.object(pipe, "_handle_command", return_value="RETRIED") as hc:
            out = "".join(pipe._execute_nl_action(pend, chat_id="c1"))
        hc.assert_called_once_with("/exec retry job-7 T3", chat_id="c1")

    def test_cleanup_dispatches_command(self, pipe):
        pend = {"intent": "cleanup", "slots": {}}
        with patch.object(pipe, "_handle_command", return_value="REAPED") as hc:
            out = "".join(pipe._execute_nl_action(pend, chat_id="c1"))
        assert out == "REAPED"
        hc.assert_called_once_with("/cleanup", chat_id="c1")

    @pytest.mark.parametrize("intent,needle", [
        ("confirm_job", "not started"), ("execute_job", "nothing was executed"),
        ("cancel_job", "left running"), ("skip_node", "left as-is"),
        ("retry_node", "left as-is"), ("cleanup", "reaper did not run"),
    ])
    def test_cancel_messages(self, pipe, intent, needle):
        msg = pipe._render_confirm_cancelled({"intent": intent, "slots": {}})
        assert needle in msg.lower()


@pytest.mark.smoke
class TestPipeWorkflowFlow:
    def test_cancel_confirm_then_affirmative_fires(self, pipe):
        clf = {"intent": "cancel_job", "confidence": "high", "job_ref": "kube"}
        resolved = ({"job_id": "job-3", "title": "kube", "status": "running"}, False,
                    [{"job_id": "job-3", "title": "kube"}])
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE"), \
             patch.object(pipe, "_classify_command", return_value=clf), \
             patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_handle_command") as hc:
            card = "".join(pipe.pipe("cancel the kube job", "m",
                                     _hist("cancel the kube job"), {}))
        assert "NL_CONFIRM:" in card and "job-3" in card
        hc.assert_not_called()                       # nothing cancelled yet

        history = [
            {"role": "user", "content": "cancel the kube job"},
            {"role": "assistant", "content": card},
            {"role": "user", "content": "yes"},
        ]
        with patch.object(pipe, "_assist_recall", return_value=None), \
             patch.object(pipe, "_call_triage", return_value="TRIAGE") as triage, \
             patch.object(pipe, "_classify_command") as clf2, \
             patch.object(pipe, "_handle_command", return_value="CANCELLED") as hc2:
            out = "".join(pipe.pipe("yes", "m", history, {}))
        assert "CANCELLED" in out
        triage.assert_not_called()
        clf2.assert_not_called()
        hc2.assert_called_once_with("/cancel job-3", chat_id=None)


# ===========================================================================
# §17.658 — Phase 7: ground-truth KB + prompt inspection
# ===========================================================================


@pytest.mark.smoke
class TestPhase7Fast:
    @pytest.mark.parametrize("msg,intent", [
        ("list ground truths", "gt_list"),
        ("show my ground truths", "gt_list"),
        ("ground truth stats", "gt_stats"),
        ("gt stats", "gt_stats"),
    ])
    def test_fast_phrases(self, msg, intent):
        assert _mod._fast_classify_command(msg) == intent


@pytest.mark.smoke
class TestPhase7RequiredSlots:
    @pytest.mark.parametrize("intent", ["gt_search", "gt_extract", "prompts_view"])
    def test_missing_slot_falls_through(self, pipe, intent):
        clf = {"intent": intent, "confidence": "high"}   # no query/topic/job_ref
        with patch.object(pipe, "_classify_command", return_value=clf):
            assert pipe._nl_command_route("do a thing", _hist("x")) is None


@pytest.mark.smoke
class TestGtReads:
    def test_gt_list_renders(self, pipe):
        body = {"total": 2, "entries": [
            {"entry_id": "e1", "title": "ZFS ARC", "tags": "zfs", "snippet": "arc cache"},
            {"entry_id": "e2", "title": "K8s DNS", "tags": "k8s", "snippet": "coredns"}]}
        with patch.object(pipe, "_gt_json", return_value=(body, None)):
            out = pipe._nl_gt_list()
        assert "Ground-truth KB" in out and "`e1`" in out and "ZFS ARC" in out

    def test_gt_list_empty_state(self, pipe):
        with patch.object(pipe, "_gt_json", return_value=({"total": 0, "entries": []}, None)):
            out = pipe._nl_gt_list()
        assert "no entries" in out.lower()

    def test_gt_search_renders_and_calls_endpoint(self, pipe):
        body = {"results": [{"entry_id": "e9", "title": "ZFS", "score": 0.87, "snippet": "x"}]}
        with patch.object(pipe, "_gt_json", return_value=(body, None)) as gj:
            out = pipe._nl_gt_search("zfs arc")
        assert "`e9`" in out and "0.87" in out
        # posts the query to /gt/search
        args, kwargs = gj.call_args
        assert args[0] == "POST" and args[1] == "/gt/search"
        assert kwargs["json_body"]["query"] == "zfs arc"

    def test_gt_stats_renders(self, pipe):
        body = {"total_entries": 42, "domains": {"eng": 30, "ops": 12}, "tags": {"zfs": 5}}
        with patch.object(pipe, "_gt_json", return_value=(body, None)):
            out = pipe._nl_gt_stats()
        assert "42 entries" in out and "eng: 30" in out

    def test_gt_read_surfaces_error(self, pipe):
        with patch.object(pipe, "_gt_json", return_value=(None, "⚠️ boom")):
            assert "boom" in pipe._nl_gt_list()


@pytest.mark.smoke
class TestGtExtractConfirm:
    def test_confirm_card_not_launch(self, pipe):
        with patch.object(pipe, "_gt_json") as gj:
            out = "".join(pipe._dispatch_nl_command(
                "gt_extract", {"topic": "zfs tuning"}, "raw"))
        assert "NL_CONFIRM:" in out and "zfs tuning" in out
        gj.assert_not_called()                 # nothing extracted until confirmed

    def test_execute_extracted_summary(self, pipe):
        result = {"status": "extracted", "entry_count": 7, "search_results_used": 12}
        with patch.object(pipe, "_gt_json", return_value=(result, None)) as gj:
            out = "".join(pipe._execute_nl_action(
                {"intent": "gt_extract", "slots": {"topic": "zfs"}}))
        assert "Extracted **7**" in out
        args, kwargs = gj.call_args
        assert args[0] == "POST" and args[1] == "/gt"
        assert kwargs["json_body"]["topic"] == "zfs"

    def test_execute_non_extracted_status(self, pipe):
        with patch.object(pipe, "_gt_json",
                          return_value=({"status": "empty", "error": "no entries"}, None)):
            out = "".join(pipe._execute_nl_action(
                {"intent": "gt_extract", "slots": {"topic": "zfs"}}))
        assert "empty" in out and "no entries" in out

    def test_cancel_message(self, pipe):
        msg = pipe._render_confirm_cancelled({"intent": "gt_extract", "slots": {}})
        assert "no ground truths" in msg.lower()


@pytest.mark.smoke
class TestPromptsView:
    def test_unique_match_renders_prompts(self, pipe):
        resolved = ({"job_id": "job-5", "title": "proxmox"}, False,
                    [{"job_id": "job-5", "title": "proxmox"}])
        pbody = {"node_count": 2, "nodes": [
            {"execution_order": 1, "node_key": "T1", "has_template": True,
             "has_optimized": False, "status": "done"},
            {"execution_order": 2, "node_key": "T2", "has_template": True,
             "has_optimized": True, "status": "pending"}]}
        with patch.object(pipe, "_resolve_job_ref", return_value=resolved), \
             patch.object(pipe, "_gt_json", return_value=(pbody, None)) as gj:
            out = "".join(pipe._nl_prompts_view({"job_ref": "proxmox"}))
        assert "Prompts — proxmox" in out and "`T1`" in out and "`T2`" in out
        assert gj.call_args[0][1] == "/prompts/job-5"

    def test_ambiguous_lists(self, pipe):
        cands = [{"job_id": "a", "title": "p one", "status": "done"},
                 {"job_id": "b", "title": "p two", "status": "done"}]
        with patch.object(pipe, "_resolve_job_ref", return_value=(cands[0], True, cands)):
            out = "".join(pipe._nl_prompts_view({"job_ref": "p"}))
        assert "ASSIST_PICK" not in out and "/prompts <id>" in out

    def test_no_match_clarifies(self, pipe):
        with patch.object(pipe, "_resolve_job_ref", return_value=(None, False, [])):
            out = "".join(pipe._nl_prompts_view({"job_ref": "nope"}))
        assert "couldn't find a job" in out.lower()
