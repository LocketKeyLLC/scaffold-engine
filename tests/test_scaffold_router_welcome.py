"""§17.300 — first-turn welcome preamble.

Goal: a brand-new operator who opens an OWUI chat with Scaffold Engine
and types natural-language sees a brief "here's how this works" block
above the triage response. The preamble fires once per chat, only on
natural-language input, only on the first user turn.

Contract pins:

  - First user-message + natural language → preamble appears, then
    triage runs normally.
  - First user-message + slash command → no preamble (operator using
    commands already knows the surface).
  - Second+ user-message → no preamble (already past first touch).
  - `show_welcome_on_first_turn = False` → no preamble regardless.
  - Preamble carries the canonical jump-in commands (`/idea`,
    `/research`, `/jobs`, `/help`) so a quick scan teaches the surface.
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


@pytest.fixture(autouse=True)
def _enable_welcome(pipe):
    """Most tests assume the valve is on (default). Tests that need it off
    flip it inline."""
    pipe.valves.show_welcome_on_first_turn = True
    yield


def _drive_pipe(pipe, user_message: str, messages: list[dict]) -> str:
    """Run pipe() and join the chunks. Triage is stubbed so we don't hit
    the LLM. §17.633 — the welcome preamble is the brand-new-operator path
    (no in-progress work); stub the cross-chat continuity so these tests
    isolate the welcome from whatever in-progress jobs exist live (the
    in-progress banner takes precedence over the welcome when work exists)."""
    with patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT"), \
         patch.object(pipe, "_reconnect_in_progress", return_value=None), \
         patch.object(pipe, "_in_progress_banner", return_value=""):
        chunks = list(pipe.pipe(user_message, "model-id", messages, {}))
    return "".join(chunks)


@pytest.mark.smoke
class TestFirstTurnWelcomeFires:
    """§17.300 — the load-bearing case."""

    def test_first_natural_language_message_gets_welcome(self, pipe):
        out = _drive_pipe(
            pipe,
            "I want to build a markdown linter",
            messages=[{"role": "user", "content": "I want to build a markdown linter"}],
        )
        assert "Welcome to Scaffold Engine" in out, (
            "§17.300: first-turn natural-language input must surface the "
            "welcome preamble. Operator can't discover the canonical "
            "/idea / /go / /research flow otherwise."
        )

    def test_welcome_appears_before_triage_output(self, pipe):
        """Preamble must yield first; triage second. Order matters for
        UX — the preamble is meant as a header, not a footer."""
        out = _drive_pipe(
            pipe,
            "anything",
            messages=[{"role": "user", "content": "anything"}],
        )
        welcome_idx = out.index("Welcome to Scaffold Engine")
        triage_idx = out.index("TRIAGE_OUTPUT")
        assert welcome_idx < triage_idx

    def test_welcome_carries_canonical_jump_in_commands(self, pipe):
        """Pin the operator-facing surface — the preamble must name
        /idea, /research, /jobs, and /help. These four are the
        smallest discovery set; removing one degrades first-touch."""
        out = _drive_pipe(
            pipe,
            "anything",
            messages=[{"role": "user", "content": "anything"}],
        )
        for cmd in ("/idea", "/research", "/jobs", "/help"):
            assert cmd in out, (
                f"§17.300: welcome preamble no longer surfaces `{cmd}`. "
                f"The 4 commands (`/idea`, `/research`, `/jobs`, `/help`) "
                f"are the canonical first-touch discovery set; dropping "
                f"one degrades the operator's path to value."
            )


@pytest.mark.smoke
class TestWelcomeSkipsWhenNotApplicable:
    """§17.300 — every case where the preamble must NOT fire."""

    def test_slash_command_first_turn_skips_welcome(self, pipe):
        """An operator who already knows slash commands shouldn't see
        the welcome — the slash command itself is the signal."""
        with patch.object(pipe, "_handle_command", return_value="HELP_OUTPUT"):
            out = "".join(pipe.pipe(
                "/help", "model-id",
                [{"role": "user", "content": "/help"}], {},
            ))
        assert "Welcome to Scaffold Engine" not in out
        assert "HELP_OUTPUT" in out

    def test_second_turn_skips_welcome(self, pipe):
        """The welcome is one-time per chat. Second turn = already past
        the first-touch moment."""
        out = _drive_pipe(
            pipe,
            "another question",
            messages=[
                {"role": "user", "content": "first message"},
                {"role": "assistant", "content": "first response"},
                {"role": "user", "content": "another question"},
            ],
        )
        assert "Welcome to Scaffold Engine" not in out
        assert "TRIAGE_OUTPUT" in out

    def test_assistant_first_does_not_count(self, pipe):
        """OWUI may pre-seed a chat with an assistant greeting. We
        count USER messages only — an assistant-first chat where the
        user sends their first user message still gets the welcome."""
        out = _drive_pipe(
            pipe,
            "I want to build something",
            messages=[
                {"role": "assistant", "content": "OWUI greeting"},
                {"role": "user", "content": "I want to build something"},
            ],
        )
        assert "Welcome to Scaffold Engine" in out

    def test_valve_disabled_skips_welcome(self, pipe):
        """Admins who want to skip the preamble globally can disable
        it via the valve."""
        pipe.valves.show_welcome_on_first_turn = False
        out = _drive_pipe(
            pipe,
            "anything",
            messages=[{"role": "user", "content": "anything"}],
        )
        assert "Welcome to Scaffold Engine" not in out
        # Triage still runs.
        assert "TRIAGE_OUTPUT" in out


@pytest.mark.smoke
class TestIsFirstTurnHelper:
    """§17.300 — the helper that drives the dispatch."""

    def test_empty_messages_is_first_turn(self, pipe):
        assert pipe._is_first_turn([]) is True

    def test_none_is_first_turn(self, pipe):
        assert pipe._is_first_turn(None) is True

    def test_single_user_message_is_first_turn(self, pipe):
        assert pipe._is_first_turn(
            [{"role": "user", "content": "hi"}]
        ) is True

    def test_assistant_only_is_first_turn(self, pipe):
        """No user messages yet — counts as first turn (the user's
        about-to-send message will be the first)."""
        assert pipe._is_first_turn(
            [{"role": "assistant", "content": "pre-seed greeting"}]
        ) is True

    def test_two_user_messages_is_not_first_turn(self, pipe):
        assert pipe._is_first_turn([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]) is False


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.300 — anchor the preamble against drive-by removal."""

    def test_welcome_preamble_anchored_in_source(self):
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            src = f.read()
        assert "Welcome to Scaffold Engine" in src, (
            "§17.300 regression: the welcome preamble has been removed "
            "from scaffold_router.py. First-touch operators land on a "
            "triage reply with no path-to-value signposting."
        )
        # Each canonical jump-in command must be present in the
        # preamble's source surface.
        for cmd in ("`/idea ", "`/research ", "`/jobs`", "`/help`"):
            assert cmd in src, (
                f"§17.300 regression: the canonical first-touch command "
                f"{cmd!r} is missing from scaffold_router.py. The 4-command "
                f"discovery set (/idea, /research, /jobs, /help) is the "
                f"audit invariant — dropping one degrades first-touch."
            )

    def test_valve_default_is_true(self):
        """First-touch must be ON by default. An OFF default would make
        the audit fix invisible until an admin enables it."""
        from pipelines import scaffold_router
        with open(scaffold_router.__file__, encoding="utf-8") as f:
            src = f.read()
        assert "show_welcome_on_first_turn: bool = True" in src, (
            "§17.300 regression: the welcome valve default is no longer "
            "True. Default-off would hide the first-touch UX behind an "
            "admin toggle — the audit invariant is that first-touch is "
            "the default behavior."
        )
