"""§17.562 — guided/minimal surface + DB-derived core verbs.

Covers the interaction-layer rework:
  - /advanced gate: non-core commands blocked when advanced mode is off
  - /advanced on|off toggle
  - /here, /next, /resume (DB-derived from GET /work, no UUID)
  - status footer: appended to lookup replies, never inside an SSE stream
  - umbrella assist guidance is NOT in this file (vendored handler) — see
    the orchestrator-side test_assist_agent.py for the gate itself.

Run: pytest --noconftest tests/test_scaffold_router_guided.py
"""
from unittest.mock import MagicMock

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _work(jobs=None, sessions=None) -> dict:
    return {
        "jobs": jobs or [],
        "assist_sessions": sessions or [],
        "timestamp": "2026-06-20T00:00:00+00:00",
    }


def _job(jid="11111111-1111-4111-8111-111111111111", title="Build a homelab",
         status="planning", phase="Plan", next_actions=None) -> dict:
    return {
        "id": jid, "title": title, "status": status, "phase": phase,
        "job_type": "legacy", "node_count": 3,
        "updated_at": "2026-06-20T00:00:00+00:00",
        "next_actions": next_actions if next_actions is not None else [
            {"action": "confirm", "command": f"/confirm {jid}",
             "endpoint": "/ideate/confirm", "method": "POST",
             "description": "Approve.", "node_specific": False},
        ],
    }


def _session(sid="22222222-2222-4222-8222-222222222222",
             job_id="11111111-1111-4111-8111-111111111111",
             title="Build a homelab", node="T1") -> dict:
    return {
        "session_id": sid, "job_id": job_id, "job_title": title,
        "status": "active", "current_node_key": node,
        "last_activity_at": "2026-06-20T00:00:00+00:00",
    }


# ── Gate ───────────────────────────────────────────────────────────────


class TestGate:

    def test_core_command_not_gated(self, pipe):
        pipe.valves.advanced_commands_enabled = False
        assert pipe._gate_advanced("/here") is None
        assert pipe._gate_advanced("/results abc") is None
        assert pipe._gate_advanced("/go build a thing") is None

    def test_assist_subcommand_resolves_to_core_base(self, pipe):
        pipe.valves.advanced_commands_enabled = False
        assert pipe._gate_advanced("/assist next") is None
        assert pipe._gate_advanced("/assist/next") is None

    def test_advanced_command_gated_when_off(self, pipe):
        pipe.valves.advanced_commands_enabled = False
        hint = pipe._gate_advanced("/jobs")
        assert hint is not None and "/advanced on" in hint
        assert pipe._gate_advanced("/research/list") is not None

    def test_advanced_command_allowed_when_on(self, pipe):
        pipe.valves.advanced_commands_enabled = True
        assert pipe._gate_advanced("/jobs") is None
        assert pipe._gate_advanced("/research/list") is None

    def test_unknown_command_falls_through(self, pipe):
        """Unknown commands return None so the existing suggester still runs."""
        pipe.valves.advanced_commands_enabled = False
        assert pipe._gate_advanced("/frobnicate") is None


class TestAdvancedToggle:

    def test_on_enables(self, pipe):
        pipe.valves.advanced_commands_enabled = False
        out = pipe._handle_advanced("/advanced on")
        assert pipe.valves.advanced_commands_enabled is True
        assert "enabled" in out.lower()

    def test_off_disables(self, pipe):
        pipe.valves.advanced_commands_enabled = True
        out = pipe._handle_advanced("/advanced off")
        assert pipe.valves.advanced_commands_enabled is False
        assert "disabled" in out.lower()

    def test_bare_reports_state(self, pipe):
        pipe.valves.advanced_commands_enabled = False
        out = pipe._handle_advanced("/advanced")
        assert "off" in out.lower()


# ── /here, /next ───────────────────────────────────────────────────────


class TestHereNext:

    def test_here_empty(self, pipe):
        assert "Nothing in progress" in pipe._render_here(_work())

    def test_here_lists_job_and_session(self, pipe):
        out = pipe._render_here(_work(jobs=[_job()], sessions=[_session()]))
        assert "Build a homelab" in out
        assert "Plan" in out               # phase label
        assert "/confirm" in out           # top next action
        assert "Assist sessions" in out
        assert "T1" in out                 # current node

    def test_next_prefers_assist_session(self, pipe):
        out = pipe._render_next(_work(jobs=[_job()], sessions=[_session()]))
        assert "assist session" in out.lower()

    def test_next_job_action(self, pipe):
        out = pipe._render_next(_work(jobs=[_job()]))
        assert "/confirm" in out

    def test_next_empty(self, pipe):
        assert "Nothing in progress" in pipe._render_next(_work())

    def test_top_action_skips_wait(self, pipe):
        actions = [
            {"action": "wait", "command": None},
            {"action": "confirm", "command": "/confirm X"},
        ]
        assert pipe._top_action_cmd(actions, "X") == "`/confirm X`"
        assert pipe._top_action_cmd([], "X") == "—"


# ── /resume ────────────────────────────────────────────────────────────


class TestResume:

    def test_resume_none(self, pipe):
        pipe._fetch_work = lambda: _work()
        out = "".join(pipe._handle_resume())
        assert "Nothing to resume" in out

    def test_resume_single_session_enters_assist(self, pipe):
        pipe._fetch_work = lambda: _work(sessions=[_session()])
        called = {}

        def _fake_next(sid, chat_id=None):
            called["sid"] = sid
            yield "STEP-RENDERED"
        pipe._assist_next = _fake_next
        out = "".join(pipe._handle_resume())
        assert called["sid"] == "22222222-2222-4222-8222-222222222222"
        assert "STEP-RENDERED" in out

    def test_resume_single_job_shows_next(self, pipe):
        pipe._fetch_work = lambda: _work(jobs=[_job()])
        out = "".join(pipe._handle_resume())
        assert "/confirm" in out

    def test_resume_multiple_lists(self, pipe):
        pipe._fetch_work = lambda: _work(
            jobs=[_job(), _job(jid="33333333-3333-4333-8333-333333333333",
                              title="Second")],
        )
        out = "".join(pipe._handle_resume())
        assert "more than one" in out.lower()
        assert "Second" in out


# ── Status footer ──────────────────────────────────────────────────────


class TestFooter:

    def test_footer_disabled_returns_empty(self, pipe):
        pipe.valves.status_footer_enabled = False
        pipe._fetch_work = lambda: _work(jobs=[_job()])
        assert pipe._status_footer() == ""

    def test_footer_empty_when_no_work(self, pipe):
        pipe.valves.status_footer_enabled = True
        pipe._fetch_work = lambda: _work()
        assert pipe._status_footer() == ""

    def test_footer_renders_next(self, pipe):
        pipe.valves.status_footer_enabled = True
        pipe._fetch_work = lambda: _work(jobs=[_job()])
        out = pipe._status_footer()
        assert out.startswith("\n\n---")
        assert "/confirm" in out

    def test_footer_commands_membership(self):
        from scaffold_router import _FOOTER_COMMANDS
        assert "/results" in _FOOTER_COMMANDS
        assert "/help" not in _FOOTER_COMMANDS


# ── pipe() dispatch integration ────────────────────────────────────────


class TestPipeDispatch:

    def test_pipe_here(self, pipe):
        pipe._fetch_work = lambda: _work(jobs=[_job()])
        out = "".join(pipe.pipe("/here", "m", [], {}))
        assert "Your active work" in out
        assert "Build a homelab" in out

    def test_pipe_gates_advanced_command(self, pipe):
        pipe.valves.advanced_commands_enabled = False
        out = "".join(pipe.pipe("/jobs", "m", [], {}))
        assert "advanced command" in out.lower()
        assert "/advanced on" in out

    def test_pipe_advanced_on_then_jobs_dispatches(self, pipe):
        # /advanced on toggles; afterwards /jobs is no longer gated. We only
        # assert the toggle here (the /jobs path hits the network).
        out = "".join(pipe.pipe("/advanced on", "m", [], {}))
        assert pipe.valves.advanced_commands_enabled is True
        assert "enabled" in out.lower()

    def test_pipe_resume_single_job(self, pipe):
        pipe._fetch_work = lambda: _work(jobs=[_job()])
        out = "".join(pipe.pipe("/resume", "m", [], {}))
        assert "/confirm" in out
