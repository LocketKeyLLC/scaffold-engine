"""§17.893 — banned-value enforcement on generated guide/fix output.

The incident that forced this: with the §17.892 pin removed AND the
constraint verbatim in the prompt ("DarthSidious is reserved for the HP
switch"), the guide model still copied the name from the prior walkthrough in
its conversation window into `qm create --name` — three times, including the
verification check. §17.882's lesson again: prompts are guidance, code is
enforcement. `environment.banned_values` ([{value, reason}]) is checked
deterministically; violations get one named-violation redraw, then a visible
flag.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_environment as E
from app.modules import assist_guide

pytestmark = pytest.mark.asyncio

_BANNED = [{"value": "DarthSidious", "reason": "reserved for the HP switch"}]


# ── find_banned_values ───────────────────────────────────────────────────────

def _sync(coro_none=None):
    return None


async def test_find_banned_values_hits_word_boundary_case_insensitive():
    text = "```bash\nqm create 106 --name DarthSidious --memory 8192\n```"
    hits = assist_guide.find_banned_values(text, _BANNED)
    assert hits == [{"value": "DarthSidious", "reason": "reserved for the HP switch"}]
    # case-insensitive
    assert assist_guide.find_banned_values("name it darthsidious", _BANNED)
    # word boundary — a different hyphenated identifier is not a hit
    assert assist_guide.find_banned_values("DarthSidious-2 is fine", _BANNED) == []


async def test_find_banned_values_ignores_short_and_empty():
    assert assist_guide.find_banned_values("use ab here", [{"value": "ab", "reason": ""}]) == []
    assert assist_guide.find_banned_values("", _BANNED) == []
    assert assist_guide.find_banned_values("anything", []) == []
    assert assist_guide.find_banned_values("anything", None) == []


async def test_banned_values_warning_names_value_and_reason():
    w = assist_guide.banned_values_warning(
        [{"value": "DarthSidious", "reason": "reserved for the HP switch"}])
    assert "DarthSidious" in w and "reserved for the HP switch" in w
    assert assist_guide.banned_values_warning([]) == ""


# ── enforce_banned_values ────────────────────────────────────────────────────

def _resp(text):
    r = MagicMock()
    r.success = True
    r.text = text
    return r


async def test_enforce_clean_draft_is_untouched():
    with patch.object(assist_guide, "chat_until_nonempty", new=AsyncMock()) as c:
        out, hits, redrew = await assist_guide.enforce_banned_values(
            text_out="qm create 106 --name palworld",
            environment={"banned_values": _BANNED},
            messages=[{"role": "user", "content": "u"}], role="model_general",
            label="t",
        )
    c.assert_not_awaited()
    assert out == "qm create 106 --name palworld" and hits == [] and redrew is False


async def test_enforce_redraws_once_and_returns_clean():
    with patch.object(assist_guide, "chat_until_nonempty",
                      new=AsyncMock(return_value=_resp("qm create 106 --name palworld"))) as c:
        out, hits, redrew = await assist_guide.enforce_banned_values(
            text_out="qm create 106 --name DarthSidious",
            environment={"banned_values": _BANNED},
            messages=[{"role": "user", "content": "u"}], role="model_general",
            label="t",
        )
    c.assert_awaited_once()
    # the redraw prompt names the violation and its reason
    sent = c.call_args.args[1]
    assert any("DarthSidious" in m["content"] for m in sent if m["role"] == "user")
    assert out == "qm create 106 --name palworld" and hits == [] and redrew is True


async def test_enforce_still_dirty_returns_remaining_hits():
    with patch.object(assist_guide, "chat_until_nonempty",
                      new=AsyncMock(return_value=_resp("still --name DarthSidious"))):
        out, hits, redrew = await assist_guide.enforce_banned_values(
            text_out="qm create --name DarthSidious",
            environment={"banned_values": _BANNED},
            messages=[], role="model_general", label="t",
        )
    assert redrew is True and hits and hits[0]["value"] == "DarthSidious"
    assert "DarthSidious" in out  # caller appends the visible flag


async def test_enforce_model_failure_keeps_original_with_hits():
    with patch.object(assist_guide, "chat_until_nonempty",
                      new=AsyncMock(side_effect=RuntimeError("down"))):
        out, hits, redrew = await assist_guide.enforce_banned_values(
            text_out="--name DarthSidious",
            environment={"banned_values": _BANNED},
            messages=[], role="model_general", label="t",
        )
    assert out == "--name DarthSidious" and hits and redrew is False


# ── environment storage ──────────────────────────────────────────────────────

def _db_with_metadata(metadata):
    res = MagicMock()
    res.mappings.return_value.first.return_value = {"metadata": metadata}
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    return db


async def test_set_environment_merges_banned_values_by_value():
    db = _db_with_metadata({"environment": {
        "banned_values": [{"value": "DarthSidious", "reason": "old reason"}],
    }})
    out = await E.set_environment(
        session_id="s1",
        banned_values=[{"value": "darthsidious", "reason": "reserved for the HP switch"},
                       {"value": "vmbr9", "reason": "does not exist"}],
        db=db,
    )
    vals = {b["value"].lower(): b["reason"] for b in out["banned_values"]}
    assert vals == {"darthsidious": "reserved for the HP switch",
                    "vmbr9": "does not exist"}


async def test_fact_fold_does_not_clobber_banned_values():
    """§17.881b regression shape for the new key."""
    db = _db_with_metadata({"environment": {
        "banned_values": [{"value": "DarthSidious", "reason": "switch"}],
    }})
    await E.set_environment(session_id="s1", facts=["new fact"], db=db)
    patch_json = json.loads(db.execute.call_args.args[1]["patch"])
    assert patch_json["environment"]["banned_values"] == [
        {"value": "DarthSidious", "reason": "switch"}]
