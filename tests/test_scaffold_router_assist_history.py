"""§17.687 — the pipeline builds the recent-conversation slice it forwards with
an assist turn (Pipeline._assist_history).

Excludes the current utterance (sent separately as the message/refine/error),
windows to the assist_history_turns valve, and caps each message so a long
pasted walkthrough can't bloat the HTTP payload.

Pipeline tests load the module in isolation via _scaffold_router_setup.
"""
import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _msgs(*pairs):
    return [{"role": r, "content": c} for r, c in pairs]


@pytest.mark.smoke
def test_assist_history_excludes_current_turn(pipe):
    msgs = _msgs(
        ("user", "set up a media server"),
        ("assistant", "I'd lean Jellyfin — free and self-hosted."),
        ("user", "yes, tell me about that one"),  # current turn
    )
    hist = pipe._assist_history(msgs)
    # The current (last) turn is NOT forwarded.
    assert all("tell me about that one" not in m["content"] for m in hist)
    assert any("Jellyfin" in m["content"] for m in hist)
    assert hist[0]["role"] in ("user", "assistant")


def test_assist_history_windows_to_valve(pipe):
    pipe.valves.assist_history_turns = 2
    msgs = _msgs(
        ("user", "OLDEST"),
        ("assistant", "a"),
        ("user", "b"),
        ("assistant", "c"),
        ("user", "current"),
    )
    hist = pipe._assist_history(msgs)
    assert len(hist) == 2
    assert "OLDEST" not in "".join(m["content"] for m in hist)


def test_assist_history_zero_valve_is_empty(pipe):
    pipe.valves.assist_history_turns = 0
    msgs = _msgs(("assistant", "prior"), ("user", "current"))
    assert pipe._assist_history(msgs) == []


def test_assist_history_caps_long_message(pipe):
    pipe.valves.assist_history_turns = 6
    big = "Z" * 9000
    msgs = _msgs(("assistant", big), ("user", "current"))
    hist = pipe._assist_history(msgs)
    assert len(hist) == 1
    assert "…[truncated]" in hist[0]["content"]
    assert len(hist[0]["content"]) <= pipe._ASSIST_HISTORY_PER_MSG_CHARS + 40


def test_assist_history_no_prior_is_empty(pipe):
    # Only the current turn — nothing prior to forward.
    assert pipe._assist_history(_msgs(("user", "hi"))) == []
    assert pipe._assist_history([]) == []
