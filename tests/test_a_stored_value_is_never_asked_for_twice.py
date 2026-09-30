"""§17.1222 — label a value once, use it everywhere.

    "Entering secrets should be similar, like labeling it 'mass password' then
     applying it across the project."

Every part of that existed already: an encrypted store keyed by name
(§17.1193), a `$NAME` reference the engine writes into commands, and
out-of-band delivery so the value reaches the machine without passing through
the engine (§17.1191). What did not exist was the DRAFTER knowing the names —
so it wrote "enter the password" into step after step for a password the
operator had typed once and labelled.

Names only. A value never enters a prompt, a block, a transcript or a log.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.modules import supervised_runs as sr


@pytest.mark.asyncio
async def test_the_stored_names_reach_the_drafter():
    with patch("app.modules.runner_secrets.list_secrets",
               new=AsyncMock(return_value=[{"name": "MASS_PASSWORD", "hint": "the one password"}])):
        got = await sr.known_secret_names()
    assert got == [{"name": "MASS_PASSWORD", "hint": "the one password"}]


@pytest.mark.asyncio
async def test_an_unreadable_store_falls_back_to_asking():
    """The old behaviour, not a crash and not a silent omission."""
    with patch("app.modules.runner_secrets.list_secrets", new=AsyncMock(side_effect=RuntimeError("down"))):
        assert await sr.known_secret_names() == []


def test_the_prompt_carries_names_and_forbids_the_value():
    """§17.1224 moved the wording into `stored_values_block`, the ONE renderer
    both the drafter and the executor's node generation call — the drift this
    test was guarding against had already happened on the other path."""
    import inspect
    src = inspect.getsource(sr.stored_values_block)
    assert "VALUES ALREADY STORED" in src
    assert "NEVER ask the" in src, "the whole point: do not ask twice"
    assert "not write the value itself" in src, "a value must never enter a prompt"
    assert "${n['name']}" in src or "$" in src
    assert "await stored_values_block()" in inspect.getsource(sr.draft_runbook)


def test_only_names_and_hints_are_read_from_the_store():
    """A value must not be pulled out even internally — the store's own API
    never returns one, and this must not start expecting it to."""
    import inspect
    src = inspect.getsource(sr.known_secret_names)
    assert '"name": r.get("name")' in src and '"hint"' in src
    assert 'r.get("value")' not in src and '"value"' not in src


@pytest.mark.asyncio
async def test_a_drafter_that_cannot_read_the_store_still_drafts():
    """A store failure must not stop a runbook: the block renders empty and the
    drafter appends it unconditionally, so the prompt is simply the old one."""
    import inspect
    src = inspect.getsource(sr.stored_values_block)
    i = src.index("known_secret_names()")
    assert "except Exception" in src[i:i + 400]
    with patch.object(sr, "known_secret_names", AsyncMock(side_effect=RuntimeError("down"))):
        assert await sr.stored_values_block() == ""
    # appended with no `if` around it, so an empty block cannot skip the append
    assert "prompt += await stored_values_block()" in inspect.getsource(sr.draft_runbook)
