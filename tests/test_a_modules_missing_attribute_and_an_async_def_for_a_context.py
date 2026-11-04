"""§17.1901 — a test that reads `m.x` on a job module (the step's own included) that has no top-level `x`, and a
patch that puts an `async def` where the code enters the name with `async with`.

Live (follow-up 5b4880c0 T9, 2026-11-04 15:29 UTC, the first step after T5 finally passed): `set(wire.__all__)` and
`patch.object(wire.paper_daily_signal_proposal_batch, …)` on the entry module T9 wrote (it loads its siblings lazily
and defines neither), and `patch("app.database.async_session", new=fake_async_session)` with `async def
fake_async_session(): … return Ctx()` -- six of six sandbox failures, none read before it."""
from __future__ import annotations

from app.modules.engine_contracts import async_def_for_a_context, missing_module_attrs

PATH = "tests/test_paper_wire_engine_entry_module.py"

WIRE = '''# app/modules/paper_wire_engine_entry_module.py
import importlib
from typing import Any

PUBLIC_COMMANDS = ("run_daily_batch_command",)

def _load_module(name: str) -> Any:
    return importlib.import_module(f"app.modules.{name}")

async def run_daily_batch_command(*, db=None) -> dict:
    mod = _load_module("paper_daily_signal_proposal_batch")
    if db is None:
        from app.database import async_session
        async with async_session() as db:
            return await mod.run_daily_batch(db=db)
    return await mod.run_daily_batch(db=db)
'''

FILES = {"app/modules/paper_wire_engine_entry_module.py": WIRE}


def test_an_attribute_the_steps_own_module_lacks_is_refused():
    test = '''# tests/test_paper_wire_engine_entry_module.py
from unittest.mock import AsyncMock, patch
from app.modules import paper_wire_engine_entry_module as wire

def test_exports():
    assert set(wire.__all__) == {"run_daily_batch_command"}
    assert wire.PUBLIC_COMMANDS == ("run_daily_batch_command",)
    assert wire.__name__.endswith("entry_module")

async def test_patch():
    with patch.object(wire.paper_daily_signal_proposal_batch, "run_daily_batch", new_callable=AsyncMock):
        await wire.run_daily_batch_command(db=None)
'''
    got = [g.split(" (it has")[0] for g in missing_module_attrs({PATH: test, **FILES}, {})]
    assert got == [
        "`tests/test_paper_wire_engine_entry_module.py:6` reads `wire.__all__`, but `paper_wire_engine_entry_module` has "
        "no top-level `__all__`",
        "`tests/test_paper_wire_engine_entry_module.py:11` reads `wire.paper_daily_signal_proposal_batch`, but "
        "`paper_wire_engine_entry_module` has no top-level `paper_daily_signal_proposal_batch`"], got
    # a module that defines it: nothing to say
    assert missing_module_attrs({PATH: test.replace("wire.__all__", "wire.PUBLIC_COMMANDS")
                                 .replace("wire.paper_daily_signal_proposal_batch", "wire._load_module"), **FILES}, {}) == []


def test_an_async_def_patched_in_for_a_name_entered_with_async_with_is_refused():
    test = '''# tests/test_paper_wire_engine_entry_module.py
import contextlib
from unittest.mock import patch
from app.modules import paper_wire_engine_entry_module as wire

async def test_opens_session():
    async def fake_async_session():
        return object()

    @contextlib.asynccontextmanager
    async def good_session():
        yield object()

    with patch("app.database.async_session", new=fake_async_session):
        await wire.run_daily_batch_command(db=None)
    with patch("app.database.async_session", new=good_session):
        await wire.run_daily_batch_command(db=None)
'''
    got = [g.split(" -- ")[0] for g in async_def_for_a_context({PATH: test, **FILES}, {})]
    assert got == [
        "`tests/test_paper_wire_engine_entry_module.py:14` patches `async_session` with `fake_async_session`, an "
        "`async def`: the code enters it with `async with async_session(…)`, and calling an `async def` gives a "
        "coroutine, which `async with` refuses"], got
