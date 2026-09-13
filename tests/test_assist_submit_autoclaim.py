"""§17.1052 — a submit on the step the session already points at claims it.

The evidence-box ✓ posted straight to /submit; when the pointer had moved
without a claim (closed tab mid-guide, a goto, an API commit) the step was
'pending', the server said 409 must_claim_first, and the SPA swallowed it —
the operator's paste and ✓ did nothing and said nothing.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_submit_claims_the_pointed_at_step_instead_of_409():
    src = (ROOT / "app/modules/assist_agent.py").read_text()
    body = src[src.index("async def submit_step("):]
    body = body[:body.index("\nasync def ", 10)]
    assert "ss.current_node_key" in body
    assert 'step["status"] == "pending" and step.get("current_node_key") == node_key' in body
    assert "SET status = 'presented', presented_at = NOW()" in body
    assert "assist_submit_autoclaimed" in body
    # the 409 survives for a pending step the session does NOT point at
    assert "must_claim_first: step" in body
    assert body.index("assist_submit_autoclaimed") < body.index("must_claim_first: step")


def test_evidence_box_submit_never_swallows_an_error():
    js = (ROOT / "app/ui/static/views/assist.js").read_text()
    fn = js[js.index("async function submitEvidence("):]
    fn = fn[:fn.index("\n  async function ", 10)]
    assert "try {" in fn and "} catch (e) {" in fn
    assert 'code === "must_claim_first" && !opts.retried' in fn
    assert "await claimAndGuideNext();" in fn
    assert "Couldn't submit that for" in fn


class _Res:
    def __init__(self, row=None):
        self._row = row

    def mappings(self):
        outer = self

        class _M:
            def first(self_m):
                return outer._row
        return _M()

    def scalar(self):
        return None


@pytest.mark.asyncio
async def test_pending_step_the_session_points_at_is_claimed_then_processed():
    from unittest.mock import AsyncMock
    from app.modules import assist_agent

    db = AsyncMock()
    row = {"step_id": "x", "status": "pending", "session_id": "s1", "job_id": "j1",
           "node_key": "T10", "current_node_key": "T10"}
    db.execute.side_effect = [_Res(row), _Res(), RuntimeError("stop here")]
    with pytest.raises(RuntimeError, match="stop here"):
        await assist_agent.submit_step(session_id="s1", node_key="T10", evidence="ok", action="submit", db=db)
    claim_sql = str(db.execute.call_args_list[1].args[0])
    assert "SET status = 'presented', presented_at = NOW()" in claim_sql
    assert db.execute.call_args_list[1].args[1] == {"step_id": "x"}


@pytest.mark.asyncio
async def test_pending_step_the_session_does_not_point_at_still_409s():
    from unittest.mock import AsyncMock
    from app.modules import assist_agent

    db = AsyncMock()
    row = {"step_id": "x", "status": "pending", "session_id": "s1", "job_id": "j1",
           "node_key": "T10", "current_node_key": "T4"}
    db.execute.side_effect = [_Res(row)]
    with pytest.raises(ValueError, match=r"^must_claim_first:"):
        await assist_agent.submit_step(session_id="s1", node_key="T10", evidence="ok", action="submit", db=db)


def test_completion_updates_the_hub_status_pill():
    assist = (ROOT / "app/ui/static/views/assist.js").read_text()
    hub = (ROOT / "app/ui/static/views/job_hub.js").read_text()
    card = assist[assist.index("function renderCompletionCard("):]
    card = card[:card.index("\n  }\n") + 4]
    assert 'new CustomEvent("scaffold:job-status"' in card
    assert 'window.addEventListener("scaffold:job-status", onStatus)' in hub
    assert 'window.removeEventListener("scaffold:job-status", statusListener)' in hub
    assert "mount(pillSlot, statusBadge(job.status))" in hub[hub.index("const onStatus"):]
