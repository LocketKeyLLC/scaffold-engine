"""§17.1217 — show the step the engine is ON, not where the cursor stopped.

    "if we are on add 50, why is it on ADD 65 on the web ui? shouldn't it be
     showing the user what its doing?"

`assist_sessions.current_node_key` is SESSION state. It freezes wherever the
walkthrough stopped, and once its steps are handed to the autonomous executor it
keeps pointing at one nobody is working on. Live:

    current_node_key   ADD65   (failed, handed_off)
    ready work         ADD50, ADD82

Same family as §17.1216: the surface was reading session state for a question
only the DAG can answer.
"""
from __future__ import annotations

from pathlib import Path

from app.modules import job_outstanding as jo

NODES = [
    {"node_key": "T1", "title": "done thing", "status": "done", "depends_on": []},
    {"node_key": "ADD50", "title": "Start container 111 (control-panel)", "status": "pending",
     "depends_on": ["T1"]},
    {"node_key": "ADD82", "title": "Install the guest agent in VM 106", "status": "pending",
     "depends_on": ["T1"]},
    {"node_key": "ADD65", "title": "Verify QEMU Guest Agent responds", "status": "failed",
     "depends_on": ["T1"], "last_verification_reason": "agent not running"},
    {"node_key": "ADD21", "title": "Start PM2 in 111", "status": "pending", "depends_on": ["ADD65"]},
]


def test_next_is_the_first_step_that_can_actually_run():
    out = jo.summarize(NODES)
    assert out["next"] == {"node_key": "ADD50", "title": "Start container 111 (control-panel)"}


def test_with_nothing_runnable_next_is_the_blocker_and_says_so():
    """Not "nothing" — the operator still needs somewhere to go."""
    blocked = [n for n in NODES if n["node_key"] != "ADD50"]
    blocked = [{**n, "status": "pending"} if n["node_key"] == "ADD82" else n for n in blocked]
    blocked = [{**n, "depends_on": ["ADD65"]} if n["node_key"] == "ADD82" else n for n in blocked]
    out = jo.summarize(blocked)
    assert out["ready"] == []
    assert out["next"]["node_key"] == "ADD65" and out["next"]["blocked"] is True


def test_a_finished_job_has_no_next():
    out = jo.summarize([{**n, "status": "done"} for n in NODES])
    assert out["finished"] and out["next"] is None


# ── the surface prefers it only when the cursor is spent ─────────────────

def test_a_spent_cursor_defers_to_the_dag():
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/assist.js").read_text()
    blk = src[src.index("function workingKey("):][:700]
    assert "TERMINAL_STEP.has(cur.step_status || cur.status)" in blk, blk
    assert "session.outstanding && session.outstanding.next" in blk
    assert "(spent && nxt && nxt.node_key) ? nxt.node_key : nk" in blk, \
        "a LIVE cursor must still win — the operator is mid-step on it"


def test_the_rail_and_the_strip_both_use_it():
    """Two surfaces showed the same stale key; fixing one would have left the
    other lying."""
    src = (Path(__file__).resolve().parents[1] / "app/ui/static/views/assist.js").read_text()
    assert "railModel(steps, workingKey(session, steps)" in src, "the rail"
    assert "const nk = workingKey(session, steps);" in src, "the plan strip"


def test_the_payload_carries_next():
    import inspect
    from app.modules import job_outstanding as m
    assert '"next": nxt,' in inspect.getsource(m.summarize)


# ── §17.1103's invariant: what is SHOWN is what is ACTED ON ──────────────

def _assist_src() -> str:
    return (Path(__file__).resolve().parents[1] / "app/ui/static/views/assist.js").read_text()


def test_no_surface_reads_the_raw_cursor_behind_workingKeys_back():
    """The sweep. §17.1217's first pass fixed the DISPLAY and left Skip,
    Hand-off and Done firing at the old key — presented ADD50, acted on ADD65,
    which is worse than the bug it replaced (§17.1103: a presented step MUST
    equal the one the verbs address).

    Two readers are legitimate and named here so the exemption is deliberate:
    the definition of workingKey itself, and the "does this session have a
    cursor at all" guard before presentNext().
    """
    src = _assist_src()
    raw = [ln.strip() for ln in src.splitlines()
           if "current_node_key" in ln and "workingKey" not in ln
           and not ln.strip().startswith("//")]
    allowed = {
        "const nk = session && session.current_node_key;",
        "if (!session?.current_node_key) await presentNext();",
    }
    # the session LIST renders each session's own cursor; it has no steps to
    # judge staleness with, and "this session stopped at X" is true there.
    raw = [r for r in raw if "s.current_node_key" not in r]
    assert set(raw) <= allowed, [r for r in raw if r not in allowed]


def test_every_verb_acts_on_the_step_that_is_shown():
    src = _assist_src()
    # Anchor on the DEFINITION, not the first mention: a comment names doneNext
    # earlier in the file, and every verb label appears first in a help-text map.
    for verb in ('async function doneNext(', 'verb("⏩ Skip"', 'verb("🤝 Engine does it"'):
        i = src.index(verb)
        blk = src[i:i + 400]
        assert "workingKey(session, steps)" in blk, f"{verb} does not act on the shown step: {blk[:160]}"


def test_the_guide_asks_about_the_step_that_is_shown():
    """A guide request carrying a different node than the one on screen answers
    a question nobody asked."""
    src = _assist_src()
    assert "node_key: workingKey(session, steps) || null, history: historyForGuide()" in src
    assert "node_key: workingKey(session, steps) || null, ...body }" in src


def test_the_live_turn_is_tagged_with_the_shown_step():
    src = _assist_src()
    assert "const _wk = workingKey(session, steps); if (_wk) live.dataset.step = _wk;" in src


def test_the_rail_counts_finished_WORK_not_finished_session_steps():
    """The rail struck ADD50 through and said "131 of 131 done" while ADD50 was
    pending in the DAG and was the next thing to do. `node_status` rides in the
    same row as `step_status`; it just read the wrong one."""
    src = _assist_src()
    blk = src[src.index("export function railModel("):][:1400]
    assert "x.node_status ? NODE_DONE.has(x.node_status)" in blk, blk
    assert "TERMINAL_STEP.has(x.step_status)" in blk, "the step status stays as the fallback"
    assert 'const NODE_DONE = new Set(["done", "skipped"]);' in src


def test_the_checklist_does_not_claim_nothing_is_needed_while_work_is_ready():
    src = _assist_src()
    blk = src[src.index("checklist-quiet"):][:700]
    assert "!o.finished && o.next" in blk, blk
    assert "No open questions" in blk and "next: ${nx.node_key}" in blk
