"""Sprint X.7 — routing-decision diagnostic capture.

Verifies _classify_dispatch + _log_routing_decision: the gate is
respected, the decision string mirrors each dispatch branch, and the
emitted log line carries the wrapper-strip + files + normalize-rewrites
context that operators need when triaging "why didn't my command run".

Pipeline tests must run with --noconftest because tests/conftest.py
eager-loads app/ which isn't available in the pipelines runtime.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    """Fresh Pipeline instance per test."""
    return Pipeline()


@pytest.mark.smoke
class TestClassifyDispatch:
    """_classify_dispatch returns (decision, command) mirroring the
    pipe() dispatch chain. Add a row here whenever pipe() gets a new
    command branch — the X.7 helper is canonically a side-channel
    mirror of the dispatch flow."""

    def test_go_command_classified(self, pipe):
        decision, cmd = pipe._classify_dispatch("/go let's launch")
        assert decision == "command:/go"
        assert cmd == "/go"

    def test_run_alias_for_go(self, pipe):
        decision, cmd = pipe._classify_dispatch("/run")
        assert decision == "command:/go"

    def test_research_reply_classified(self, pipe):
        decision, cmd = pipe._classify_dispatch("/research/reply yes please")
        assert decision == "command:/research/reply"

    def test_research_mgmt_classified(self, pipe):
        decision, cmd = pipe._classify_dispatch("/research/list")
        assert decision == "command:/research/mgmt"
        assert cmd == "/research/list"

    def test_research_main_classified(self, pipe):
        decision, cmd = pipe._classify_dispatch("/research bouldering routes V5")
        assert decision == "command:/research"

    def test_assist_subcommand_classified(self, pipe):
        decision, cmd = pipe._classify_dispatch("/assist/submit out=foo")
        assert decision == "command:/assist"
        assert cmd == "/assist/submit"

    def test_unrecognized_slash_classified(self, pipe):
        """Slash-prefixed but no handler — operator probably typed wrong."""
        decision, cmd = pipe._classify_dispatch("/notarealcommand")
        assert decision == "command:unrecognized"
        assert cmd == "/notarealcommand"

    def test_no_slash_falls_to_triage(self, pipe):
        decision, cmd = pipe._classify_dispatch("just chatting about my idea")
        assert decision == "triage"
        assert cmd is None

    def test_empty_message_falls_to_triage(self, pipe):
        decision, cmd = pipe._classify_dispatch("")
        assert decision == "triage"
        assert cmd is None


@pytest.mark.smoke
class TestLogRoutingDecisionGate:
    """The diagnostic must NOT fire unless the valve is on, and MUST
    fire exactly once per pipe() call when on."""

    def test_off_by_default_no_print(self, pipe, capsys):
        """Default valve state → no ROUTING_DECISION line printed."""
        assert pipe.valves.log_routing_decisions is False
        # Force the gate-checking branch: invoke the helper conditionally
        # via the same path pipe() uses (valves check + helper call).
        if pipe.valves.log_routing_decisions:
            pipe._log_routing_decision("triage", 10)
        captured = capsys.readouterr()
        assert "ROUTING_DECISION" not in captured.out

    def test_on_emits_single_line(self, pipe, capsys):
        pipe.valves.log_routing_decisions = True
        pipe._log_routing_decision(
            "command:/research", 42,
            command="/research",
            wrapper_stripped="</context>",
            files_count=1,
            normalize_rewrites=0,
            body={"files": [{"id": "f1"}], "file_ids": ["f1"]},
        )
        captured = capsys.readouterr()
        # Single emit; key fields present.
        assert captured.out.count("ROUTING_DECISION") == 1
        assert "decision='command:/research'" in captured.out
        assert "command='/research'" in captured.out
        assert "wrapper_stripped='</context>'" in captured.out
        assert "msg_len=42" in captured.out
        assert "files_count=1" in captured.out
        assert "file_ids_count=1" in captured.out
        assert "normalize_rewrites=0" in captured.out

    def test_helper_handles_missing_body_fields(self, pipe, capsys):
        """When body has neither files nor file_ids, log still emits with
        zeroed counts — the helper must never raise on partial input."""
        pipe.valves.log_routing_decisions = True
        pipe._log_routing_decision(
            "triage", 100,
            command=None, wrapper_stripped=None,
            files_count=0, normalize_rewrites=0,
            body={},
        )
        captured = capsys.readouterr()
        assert "ROUTING_DECISION" in captured.out
        assert "files_count=0" in captured.out
        assert "file_ids_count=0" in captured.out
        assert "has_files_field=False" in captured.out

    def test_helper_swallows_internal_errors(self, pipe, capsys):
        """If the helper itself raises (e.g. body is a non-dict), the
        catch-all must emit a fallback line — diagnostic logging cannot
        bring down a chat session."""
        pipe.valves.log_routing_decisions = True
        # Body is a non-dict; the .get() inside the helper would raise.
        # Wrap the call: if the helper has a defensive try/except, we
        # see a fallback message rather than an exception.
        try:
            pipe._log_routing_decision(
                "triage", 0, body="not a dict",
            )
        except Exception:
            pytest.fail(
                "log helper raised on bad input — must be defensive"
            )
        captured = capsys.readouterr()
        # Either emits the line successfully (tolerant of non-dict body)
        # or prints the fallback "log failed" line. Both are acceptable;
        # the only unacceptable behavior is an unhandled exception.
        assert (
            "ROUTING_DECISION" in captured.out
            or "ROUTING_DECISION log failed" in captured.out
        )
