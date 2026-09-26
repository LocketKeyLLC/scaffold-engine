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
