"""§17.1176 — the OTHER reset path destroyed evidence with no copy, and a
reopen stopped at its own node.

Item 10 of the 2026-09-25 audit's fix order. §17.1056 gave `apply_note_replan`'s
reopen a pre-image, on the reasoning that "a reopen nulls the step's evidence and
the node's output with no copy kept, so a wrong reopen could not be undone".
`apply_selective_replan` performs the SAME two destructive writes over the whole
downstream subgraph — and under `policy='full'` over every non-done node — and
kept nothing. It does not exclude `committed` steps, so operator-submitted
evidence was nulled with no way back.

And a reopen was not transitively closed: resetting a done node left every node
downstream of it `done` on output derived from the result just deleted, while
§17.747's own rationale IS the transitive case.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from app.modules import assist_replan as ar

pytestmark = pytest.mark.smoke


class TestBothResetPathsKeepAPreImage:
    def test_the_capture_is_one_function_used_by_both(self):
        """Two copies of a pre-image capture is how one of them rots — the
        sibling-call-site shape this codebase has been bitten by repeatedly."""
        assert callable(ar.capture_preimages) and callable(ar.store_preimages)
        note = inspect.getsource(ar.apply_note_replan)
        sel = inspect.getsource(ar.apply_selective_replan)
        for src, which in ((note, "apply_note_replan"), (sel, "apply_selective_replan")):
            assert "capture_preimages(" in src, f"{which} captures no pre-image"
            assert "store_preimages(" in src, f"{which} stores no pre-image"

    def test_the_selective_capture_happens_BEFORE_the_reset(self):
        """A pre-image taken after the UPDATE is a copy of the damage."""
        src = inspect.getsource(ar.apply_selective_replan)
        assert src.index("capture_preimages(") < src.index("UPDATE dag_nodes"), \
            "the capture must precede the destructive write"

    def test_the_selective_path_returns_what_it_destroyed(self):
        assert '"preimages": _pre' in inspect.getsource(ar.apply_selective_replan)

    @pytest.mark.asyncio
    async def test_capture_is_fail_soft(self):
        """Insurance must never be the thing that breaks the write."""
        class Boom:
            async def execute(self, *a, **k):
                raise RuntimeError("db gone")
        assert await ar.capture_preimages(db=Boom(), session_id="s", node_keys=["T1"]) == []
        assert await ar.capture_preimages(db=Boom(), session_id="s", node_keys=[]) == []
        await ar.store_preimages(db=Boom(), session_id="s", preimages=[{"node_key": "T1"}])


class TestAReopenIsTransitivelyClosed:
    def test_apply_note_replan_walks_the_downstream_bfs(self):
        """`downstream_node_keys` — the BFS that closes it — already lived two
        functions away and was never called from the reopen path, so the closure
        depended entirely on the model enumerating every affected done node out
        of a `done_block[:6000]` that is truncated."""
        src = inspect.getsource(ar.apply_note_replan)
        assert "downstream_node_keys_many(" in src, \
            "the closure must use the ONE-query variant, not one SELECT per reopened key"
        assert "status = 'done'" in src, "only DONE descendants are reopened"

    def test_the_closure_is_fail_soft(self):
        """A closure miss must degrade to the old behaviour, not block the
        reopen the operator confirmed."""
        src = inspect.getsource(ar.apply_note_replan)
        block = src[src.index("downstream_node_keys_many("):]
        assert "reopen_closure_failed" in block[:1600]

    def test_pending_descendants_are_left_alone(self):
        """A pending downstream node has nothing to undo — reopening it would
        be noise, and `status = 'done'` in the closure query is what says so."""
        src = inspect.getsource(ar.apply_note_replan)
        closure = src[src.index("_closure = await"):src.index("preimages: list[dict]")]
        assert "AND status = 'done'" in closure


# ---------------------------------------------------------------------------
# §17.1179 (audit M10) — apply_note_replan is the shared sink for proposals
# from three producers, and every branch in it is
# `[p for p in proposals if p.get("action") == X]`. An action none of those
# match produced an all-empty result indistinguishable from "the note changed
# nothing" — §17.1170's silent-branch class, one module over.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revize", "REOPEN", "delete", "", None])
async def test_an_unknown_proposal_action_is_refused_not_ignored(action):
    from app.modules import assist_replan
    db = AsyncMock()
    with pytest.raises(ValueError, match="unknown proposal action"):
        await assist_replan.apply_note_replan(
            db=db, session_id="sid", job_id="jid",
            proposals=[{"node_key": "T1", "action": action}],
        )
    db.commit.assert_not_awaited(), "a refused call must not have written anything"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revise", "drop", "reopen", "repair", "rewrite"])
async def test_every_producers_action_is_accepted(action):
    """The refusal must not be over-tight: the state check (§17.1050 `repair`)
    and reconciliation (§17.1048 `rewrite`) are legitimate producers whose
    actions are NOT in RECORD_PLAN_IMPACT_TOOL's model-facing enum."""
    from app.modules import assist_replan
    assert action in assist_replan._KNOWN_ACTIONS


def test_the_accepted_set_covers_every_action_the_body_branches_on():
    """Keeps the declared set honest: if a new `action == "..."` branch is
    added and the constant is not, this fails rather than the new action being
    rejected at the door."""
    import pathlib
    import re
    from app.modules import assist_replan
    src = (pathlib.Path(assist_replan.__file__)).read_text()
    body = src[src.index("async def apply_note_replan"):]
    branched = set(re.findall(r'\.get\("action"\)\s*==\s*"([a-z_]+)"', body))
    assert branched, "found no action branches — has the dispatch shape changed?"
    missing = sorted(branched - assist_replan._KNOWN_ACTIONS)
    assert not missing, f"the body acts on {missing} but the guard would refuse them"
