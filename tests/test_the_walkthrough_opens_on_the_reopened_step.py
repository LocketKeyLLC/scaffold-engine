"""§17.1434 — "Walk me through it" on a job whose walkthrough session already finished opens on the steps
the plan has open again, guided fresh; and the Session panel has a way back.

Live, 2026-10-08: ADD4 (the router walkthrough) was reopened on a `blocked` job. "Walk me through it"
returned the session from 2026-08-27 -- `completed`, ADD4's step `committed`, the cursor on ADD65: no Guide
me button. The auto-guide also counted ADD4's 2026-09-11 turn, so it would not have walked the step. And
"ⓘ Session" covered the chat with only a small ✕ to return. The revive SQL was dry-run (rolled back) on the
live rows: 7 steps revived, the session active, the first step presented ADD4.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _start_src() -> str:
    src = (ROOT / "app/modules/assist_agent.py").read_text()
    return src[src.index("async def start_assist_session("):src.index("assist_session_started session_id=")]


def test_a_finished_session_is_revived_on_the_steps_the_plan_has_open():
    start = _start_src()
    i = start.index("§17.1434")
    body = start[i:i + 3500]
    assert "if not reopening:" in body
    assert "d.status = 'pending'" in body                                         # a pending plan node only
    assert "s.status IN ('committed', 'skipped', 'handed_off', 'escalated')" in body
    assert "ORDER BY d.execution_order DESC" in body                              # earliest ends as the cursor
    assert "_reopen_step_mirrored(db=db, job_id=job_id, session_id=session_id, node_key=_nk" in body   # FSM + mirror
    assert body.index("SET status = 'active'") < body.index("_reopen_step_mirrored(")                  # cursor needs active
    assert start.index("§17.1434") < start.index("if total and not pending and not reopening:")


def test_the_steps_api_carries_presented_at():
    src = (ROOT / "app/modules/assist_agent.py").read_text()
    ls = src[src.index("async def list_steps("):]
    ls = ls[:ls.index("\nasync def ", 10)]
    assert "s.presented_at" in ls


def test_the_auto_guide_counts_only_this_pass_and_the_panel_has_a_way_back():
    js = (ROOT / "app/ui/static/views/assist.js").read_text()
    ag = js[js.index("async function maybeAutoGuide()"):]
    ag = ag[:ag.index("\n  }\n")]
    assert "st.presented_at" in ag and "Date.parse(t.created_at) >= since" in ag
    assert js.count('text: "← Back to the walkthrough"') == 1 and "back(), el(\"span\"" in js
