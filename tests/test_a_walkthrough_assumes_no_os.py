"""§17.1743 — an operator walkthrough assumes no operating system, says honestly whether anything is checked, and
what the operator reports is the step's record.

Live (job f9ca3ee1 T1 "Inventory laptop hardware", 2026-10-21): laptops with NO OS yet; the walkthrough sent the operator
to Task Manager and File Explorer, promised "the engine will read the machine to confirm the inventory" (there was no
machine and no check), and "I've done it" would have recorded the step done with the inventory thrown away."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import operator_steps as ops

T1 = {"node_key": "T1", "title": "Inventory laptop hardware", "tool": "LLM", "depends_on": [],
      "description": "Ask the operator how many old laptops exist and their model, RAM, core count, and disk size. "
                     "Produce a structured inventory list."}


async def _system_for(node):
    seen = {}

    async def _gen(fn, prompt, kw, **k):
        seen["system"] = k.get("system", "")
        r = MagicMock(); r.text = "## 👉 Do this next\nx"
        return r
    with patch("app.utils.llm_retry.generate_until_nonempty", new=_gen), \
         patch("app.modules.assist_research_lib._deep_web_sources", new=AsyncMock(return_value=[])):
        frame = await ops.frame_operator(node)
    return seen["system"], frame


@pytest.mark.asyncio
async def test_a_step_with_no_check_is_told_no_os_and_that_the_answer_is_the_record():
    system, frame = await _system_for(T1)
    assert "NEVER assume an operating system" in system and "BIOS/UEFI" in system
    assert "type what they found into the note" in system and "read the machine to confirm" not in system
    assert frame["verify"] == []


@pytest.mark.asyncio
async def test_a_step_with_a_check_still_says_the_engine_checks():
    node = {**T1, "description": "The operator's part: plug laptop 1 into the switch. Done when "
                                 "`ping -c 1 192.0.2.50` answers."}
    system, frame = await _system_for(node)
    assert "read the machine to confirm" in system and "NEVER assume an operating system" in system


@pytest.mark.asyncio
async def test_what_the_operator_reports_is_recorded():
    db = AsyncMock()
    upd = MagicMock(); upd.rowcount = 1
    db.execute = AsyncMock(return_value=upd)
    waiting = {"kind": "yours", "node_key": "T1", "title": T1["title"], "part": T1["description"], "verify": []}
    res = await ops.resolve_operator(db, "j", "T1", "done", waiting, note="3 laptops: Dell E7450 8GB 2c 256GB, …")
    assert res["outcome"] == "done"
    out = db.execute.await_args_list[-1].args[1]["out"]
    assert "## What the operator reported" in out and "Dell E7450" in out


def test_the_pause_passes_the_note():
    import inspect
    from app.modules import decision_pause
    assert "note=note or \"\"" in inspect.getsource(decision_pause.resolve_decision)
