"""§17.1216 — one answer to "is this finished, and if not, what now".

Every surface computed it for itself and they disagreed. On the operator's job
at the same moment:

    job status        assisted_executing
    assist progress   131/131 steps · 100%
    assist banner     "🎉 Job complete — every step is done"
    assist footer     "Nothing needed from you right now"
    dag_nodes         62 done · 53 skipped · 14 pending · 3 FAILED

`131/131` is `committed + skipped + handed_off`, and `handed_off` means the
autonomous executor owns the step — not that it succeeded. §17.1208 fixed that
arithmetic for the JOB STATUS and left three siblings doing it the old way. The
operator found all three:

    "why do i keep having to reiterating the poor web ui layout to best reflect
     what the model needs so that the user can answer and assist in completion"
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.modules import job_outstanding as jo

# the operator's job, in miniature: a failure holding up everything behind it
NODES = [
    {"node_key": "T1", "title": "audit", "status": "done", "depends_on": []},
    {"node_key": "T2", "title": "skip me", "status": "skipped", "depends_on": ["T1"]},
    {"node_key": "ADD21", "title": "Start the PM2 process in LXC 111", "status": "failed",
     "depends_on": ["T1"], "last_verification_reason": "container '111' not running!"},
    {"node_key": "ADD52", "title": "Start the backend on 3001", "status": "pending", "depends_on": ["ADD21"]},
    {"node_key": "T37", "title": "Validate entire build", "status": "pending", "depends_on": ["ADD52"]},
    {"node_key": "ADD65", "title": "guest agent", "status": "failed", "depends_on": ["T1"]},
]


def test_handed_off_work_is_not_finished_work():
    out = jo.summarize(NODES)
    assert out["finished"] is False
    assert out["done"] == 2 and out["pending"] == 2 and out["failed"] == 2


def test_a_genuinely_finished_job_says_so():
    done = [{**n, "status": "done"} for n in NODES]
    out = jo.summarize(done)
    assert out["finished"] is True and jo.headline(out) == "Every step is done or skipped."


def test_a_blocker_reports_how_much_it_is_holding_up():
    """"3 failed" informs nobody. A failure holding up eleven steps is a
    different sentence from one holding up none."""
    out = jo.summarize(NODES)
    top = out["blockers"][0]
    assert top["node_key"] == "ADD21", out["blockers"]
    assert top["unblocks"] == 2, "ADD52 and T37 are both behind it"
    assert "container '111' not running" in top["reason"]
    assert out["blockers"][1]["unblocks"] == 0, "ADD65 blocks nothing"


def test_blockers_are_ordered_by_what_they_unblock():
    assert [b["node_key"] for b in jo.summarize(NODES)["blockers"]] == ["ADD21", "ADD65"]


def test_ready_is_what_could_actually_start():
    out = jo.summarize(NODES)
    assert out["ready"] == [], "every pending step is behind a failure"
    freed = [{**n, "status": "done"} if n["node_key"] == "ADD21" else n for n in NODES]
    assert jo.summarize(freed)["ready"] == ["ADD52"]


def test_the_headline_says_what_happens_next_not_just_what_is_wrong():
    h = jo.headline(jo.summarize(NODES))
    assert "Stopped." in h and "waiting on" in h and "ADD21" in h, h
    freed = [{**n, "status": "done"} if n["node_key"] == "ADD21" else n for n in NODES]
    assert "can run now" in jo.headline(jo.summarize(freed))


def test_an_unreadable_job_does_not_claim_finished():
    assert jo.headline({"known": False}) == ""
    assert jo.summarize([])["finished"] is True   # an empty plan genuinely has nothing left


# ── the three siblings now read it ───────────────────────────────────────

def test_the_server_progress_prefers_the_dag_over_the_step_rollup():
    import inspect
    from app.modules import assist_agent as aa
    src = inspect.getsource(aa._assist_step_progress)
    assert 'if out and out.get("known") and out.get("total"):' in src, src
    assert 'done = int(out.get("done") or 0)' in src
    # the old arithmetic survives only as the fallback
    assert "_ASSIST_STEP_TERMINAL" in src and src.index("out.get(\"known\")") < src.index("_ASSIST_STEP_TERMINAL")


def test_the_session_payload_carries_it():
    import inspect
    from app.modules import assist_agent as aa
    src = inspect.getsource(aa)
    assert '"outstanding": _o,' in src
    assert "_assist_step_progress(step_counts, _o)" in src, "progress must be told the truth too"


def test_the_completion_card_cannot_fire_over_outstanding_work():
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/assist.js").read_text()
    blk = src[src.index("function renderCompletionCard()"):][:700]
    assert "out.known && !out.finished" in blk, blk
    assert "renderOutstandingCard(" in blk, "it must show what is left instead of nothing"


def test_the_outstanding_card_names_the_blocker_and_offers_a_move():
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/assist.js").read_text()
    blk = src[src.index("function renderOutstandingCard("):][:1400]
    assert "out.headline" in blk
    assert "holding up" in blk, "say how much the blocker is costing"
    assert "Open the run →" in blk, "a card with no action is a status line"


def test_the_strip_counts_come_from_the_same_truth():
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/assist.js").read_text()
    blk = src[src.index("const _o = session.outstanding;"):][:500]
    assert "(_o && _o.known) ? _o.done" in blk, blk
    assert "handed_off" in blk, "the roll-up stays as the fallback"
