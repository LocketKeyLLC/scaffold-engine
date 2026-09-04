"""§17.908 — a contentless turn must not redraw the walkthrough.

Live (session 613dd1df, T23): the *"saved walkthrough … is from before your
recent work"* banner fired seven-plus times, each costing the operator "a minute
or two" to regenerate substantially the same text. Cause: §17.877's staleness
probe counted ANY operator turn newer than the cached guidance, and the operator
had typed "anything" repeatedly. 10 of that step's 52 operator turns carried no
content at all.
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import assist_guide

pytestmark = pytest.mark.asyncio


def _sql() -> str:
    return inspect.getsource(assist_guide.cached_guidance_is_stale)


def test_submit_and_note_always_count_as_material():
    """A one-word submit ('done') is a real state change, not chatter."""
    assert "t.kind IN ('submit', 'note')" in _sql()


def test_bare_messages_are_word_counted_against_the_trivial_set():
    sql = _sql()
    assert "regexp_split_to_array" in sql
    assert "<> ALL(:trivial)" in sql


def test_whitespace_split_uses_posix_class_not_an_e_string():
    """REGRESSION — the first cut used `E'\\s+'`. In a Postgres E-string `\\s` is
    not an escape: it collapses to the literal letter **s**, so the predicate
    split on 's' instead of whitespace. Live effect: "I want to build a markdown
    linter" contains no letter s, so it split into ONE token and was discarded as
    contentless, while "I want to build something" (an s in 'something') was
    kept. Caught only by running it against real rows. `[[:space:]]` needs no
    backslash in Python or SQL and cannot regress this way."""
    sql = _sql()
    assert "[[:space:]]+" in sql
    assert "\\s+" not in sql
    assert "E'" not in sql


def test_trivial_set_is_sourced_from_assist_memory_not_duplicated():
    """One owner for 'what counts as a contentless message' — the same predicate
    derive_turn_memory uses to decide a message is worth reading."""
    from app.modules.assist_memory import _TRIVIAL_TURN
    assert assist_guide._trivial_turns() == set(_TRIVIAL_TURN)
    assert "anything" not in _TRIVIAL_TURN  # caught by the <2-word rule instead
    assert "ok" in _TRIVIAL_TURN


def test_advance_and_replan_signals_are_untouched():
    """§17.894's session-level staleness must survive this change — those catch
    the costlier failure (a guide written before the plan moved on)."""
    sql = _sql()
    assert "AS advanced" in sql and "AS replanned" in sql


# ── §17.917 — an open step's goal is NOT achieved ────────────────────────


def test_guide_prompt_states_the_step_is_not_done():
    """Live (turn 1445): "Guide me" on "Install Ubuntu Server 22.04 on VM 106"
    returned a POST-INSTALL walkthrough. A step being guided is pending or
    presented — never committed — so its goal is unachieved, and nothing in the
    prompt said so.

    §17.921 — reframed: "assume it is not done" fights genuine ambiguity, since
    earlier attempts left the VM PARTLY changed and nobody, operator included,
    knows what is on that disk. The walkthrough must DETERMINE the state and
    handle BOTH outcomes.

    Asserted against the RENDERED prompt, not the source: the sentences are
    split across adjacent string literals, so source-text matching tests the
    formatting rather than the contract.
    """
    from types import SimpleNamespace
    from app.modules.assist_guide import _build_guide_user_prompt

    ctx = SimpleNamespace(
        assembled_prompt="Task: install the OS.",
        title="Install Ubuntu Server 22.04 on VM 106",
        tool="Shell", base_prompt="Task: install the OS.",
    )
    prompt = _build_guide_user_prompt(ctx, None, [], None)

    assert "This step is NOT done" in prompt
    assert "Install Ubuntu Server 22.04 on VM 106" in prompt
    assert "AMBIGUOUS" in prompt
    # determine-then-branch, rather than assume either state
    assert "REVEALS it" in prompt
    assert "handle BOTH outcomes" in prompt
    # and it must forbid the exact inference that produced the live failure
    assert "a disk existing is not an OS installed on it" in prompt
    assert "has already been completed, run, or performed" in prompt
