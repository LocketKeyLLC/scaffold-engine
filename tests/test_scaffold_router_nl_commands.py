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
        # Phase 2 direct (cheap/reversible) writes:
        ("model_reset", {}, "/model reset"),
        ("model_set", {"model_role": "coder", "model_name": "kimi:cloud"},
         "/model set coder kimi:cloud"),
        ("optimize", {"prompt": "write a haiku"}, "/optimize write a haiku"),
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
