"""§17.892 — node-scoped substitution storage + learning.

The DarthSidious incident: HOSTNAME auto-pinned during the HP-switch step
deterministically named the PalWorld VM after the switch. Auto-learned values
now live under ``environment.substitutions_by_node[node_key]``; only
operator-set pins stay global. These tests cover the environment merge/
round-trip (§17.881b clobber shape included) and learn_from_submit's scoping.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_environment as E

pytestmark = pytest.mark.asyncio


def _db_with_metadata(metadata):
    res = MagicMock()
    res.mappings.return_value.first.return_value = {"metadata": metadata}
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    return db


async def test_environment_round_trips_substitutions_by_node():
    env = E._environment_from_metadata({"environment": {
        "substitutions_by_node": {"T4": {"HOSTNAME": "DarthSidious"}},
    }})
    assert env["substitutions_by_node"] == {"T4": {"HOSTNAME": "DarthSidious"}}
    # absent / wrong-typed → empty dict (readers use .get; the no-environment
    # early return keeps its historical minimal shape)
    assert E._environment_from_metadata({}).get("substitutions_by_node", {}) == {}
    assert E._environment_from_metadata(
        {"environment": {"substitutions_by_node": "junk"}}
    )["substitutions_by_node"] == {}


async def test_set_environment_merges_by_node_and_deletes_empty():
    db = _db_with_metadata({"environment": {
        "substitutions_by_node": {"T1": {"K": "old", "GONE": "x"}},
    }})
    out = await E.set_environment(
        session_id="s1",
        substitutions_by_node={"T1": {"K": "new", "GONE": ""}, "T2": {"X": "y"}},
        db=db,
    )
    assert out["substitutions_by_node"] == {"T1": {"K": "new"}, "T2": {"X": "y"}}


async def test_fact_fold_does_not_clobber_by_node():
    """§17.881b regression shape — a later facts-only write must round-trip the
    node-scoped pins (a key missing from _environment_from_metadata is erased
    by the very next whole-dict env write)."""
    db = _db_with_metadata({"environment": {
        "substitutions_by_node": {"T4": {"HOSTNAME": "DarthSidious"}},
    }})
    await E.set_environment(session_id="s1", facts=["new fact"], db=db)
    patch_json = json.loads(db.execute.call_args.args[1]["patch"])
    assert patch_json["environment"]["substitutions_by_node"] == {
        "T4": {"HOSTNAME": "DarthSidious"}
    }
    assert patch_json["environment"]["facts"] == ["new fact"]


# ── learn_from_submit scoping ────────────────────────────────────────────────


def _learn_patches(es, *, existing_env, extracted):
    from app.modules import assist_agent, assist_guide
    es.enter_context(patch.object(assist_agent, "capture_execution_context",
                     new=AsyncMock(return_value=None)))
    es.enter_context(patch.object(assist_guide, "read_cached_guidance",
                     new=AsyncMock(return_value={"guidance": "name it <NAME>"})))
    es.enter_context(patch.object(assist_guide, "extract_substitutions",
                     new=AsyncMock(return_value=extracted)))
    es.enter_context(patch.object(assist_agent, "get_environment",
                     new=AsyncMock(return_value=existing_env)))
    return es.enter_context(patch.object(assist_agent, "set_environment",
                            new=AsyncMock()))


async def test_learn_stores_node_scoped():
    import contextlib
    from app.modules.assist_memory import learn_from_submit
    with contextlib.ExitStack() as es:
        se = _learn_patches(es, existing_env={
            "substitutions": {},
            # another node's pin must NOT block learning on this node
            "substitutions_by_node": {"T4": {"NAME": "DarthSidious"}},
        }, extracted={"NAME": "palworld"})
        new = await learn_from_submit(
            session_id="s1", node_key="T22", evidence="--name palworld ok", db=AsyncMock(),
        )
    assert new == {"NAME": "palworld"}
    assert se.call_args.kwargs["substitutions_by_node"] == {"T22": {"NAME": "palworld"}}
    assert "substitutions" not in se.call_args.kwargs or not se.call_args.kwargs.get("substitutions")


async def test_learn_same_node_existing_key_wins():
    import contextlib
    from app.modules.assist_memory import learn_from_submit
    with contextlib.ExitStack() as es:
        se = _learn_patches(es, existing_env={
            "substitutions": {},
            "substitutions_by_node": {"T22": {"NAME": "palworld"}},
        }, extracted={"NAME": "different"})
        new = await learn_from_submit(
            session_id="s1", node_key="T22", evidence="…", db=AsyncMock(),
        )
    assert new == {}
    se.assert_not_awaited()


async def test_learn_operator_global_pin_wins():
    import contextlib
    from app.modules.assist_memory import learn_from_submit
    with contextlib.ExitStack() as es:
        se = _learn_patches(es, existing_env={
            "substitutions": {"NAME": "operator-choice"},
            "substitutions_by_node": {},
        }, extracted={"NAME": "scraped"})
        new = await learn_from_submit(
            session_id="s1", node_key="T22", evidence="…", db=AsyncMock(),
        )
    assert new == {}
    se.assert_not_awaited()
