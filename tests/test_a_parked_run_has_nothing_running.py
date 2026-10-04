"""§17.1337 — a parked run has nothing running, and the step the stream names is
read after the pause, not before it.

Live, 2026-10-04, the operator: *"Ran Add129, it also says Add 122 is running."*
Neither was true of the engine. Only one decision had been posted (the step that
starts Palworld's VM, which ran `qm start 106` and finished), no node was in the
`running` state, the step the operator believed they ran was `pending` with no
decision recorded, and `/exec/status` named a third step as next.

Two causes, one in each half:

1. `execute_all_nodes` peeks the next step, then pauses if that step is hands-on.
   When the pause returns None it can have settled that very step (§17.1302
   records an already-met step done from the machine's own checks), reopened
   another, inserted a prerequisite (§17.1336) or reordered the plan — and the
   loop then told the stream that the STALE peek had started.
2. `node_start` marks a node running in the page, and only `node_done` or
   `node_failed` clear it. A step previewed and then parked instead of claimed
   stayed on screen as running for good.
"""
from __future__ import annotations

import inspect
import pathlib

from app.modules import execution_agent as ea


def _execute_all_body() -> str:
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def execute_all_nodes(")
    nxt = src.find("\nasync def ", i + 10)
    return src[i:nxt] if nxt != -1 else src[i:]


def test_the_stream_re_reads_the_next_step_after_a_pause():
    """Verify the LANE: the re-peek sits between the park's return and the
    `node_start` the stream sends."""
    body = _execute_all_body()
    park = body.index('yield _sse("awaiting_decision", _asked)')
    start = body.index('yield _sse("node_start"', park)
    between = body[park:start]
    assert "_peek_next_node(job_id)" in between, between[-400:]
    assert "§17.1337" in between, "with the reason, so it is not removed as a duplicate read"


def test_the_peek_is_still_read_only():
    """It must stay a snapshot: the claim is `_claim_ready_nodes`' business, and a
    view that mutates is §17.1209's bug."""
    src = inspect.getsource(ea._peek_next_node)
    assert "SELECT" in src and "UPDATE" not in src.upper(), src
    assert "status = 'pending'" in src


def test_both_peeks_name_the_same_reader():
    """One reader, so the two reads cannot drift (sibling call sites)."""
    body = _execute_all_body()
    assert body.count("await _peek_next_node(job_id)") == 2, body.count("await _peek_next_node(job_id)")


def test_the_page_clears_running_when_the_run_parks():
    """The other half lives in the SPA and is tested by
    `tests/ui/theater_park_clears_running.test.mjs`; this asserts the view really
    carries the rule, so the JS test cannot pass against a view that dropped it."""
    view = pathlib.Path(ea.__file__).parents[2] / "app" / "ui" / "static" / "views" / "theater.js"
    text = view.read_text(encoding="utf-8")
    assert "export function clearsRunning(" in text
    for event in ("awaiting_decision", "awaiting_assist", "pipeline_complete",
                  "execution_failed", "error", "budget_exhausted"):
        assert f'"{event}"' in text.split("export const CLEARS_RUNNING")[1].split("]")[0], event
    handler = text[text.index("function handleEvent("):]
    assert "clearsRunning(event)" in handler[:600], handler[:600]
    assert 'ensureNode(key, { status: "pending" })' in handler[:900]
