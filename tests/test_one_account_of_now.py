"""§17.1214 — the Run surface must give ONE account of what is happening.

The operator, on a job parked at a run approval:

    "EVERYTHING about this ui layout and flow … needs a SERIOUS fix. It does not
     clearly show what it is working on. When approached with the next step it
     does not properly resolute with the web ui."

On that page at once: the stage strip twice (the hub's, and the flow guide's own
five-stage copy), the status badge twice ("Waiting for your decision" at the top
of the page and again three inches below), a decision card saying "the run
stopped for your approval" beside a panel saying "1 step can run now", the step
under decision not lit in the 132-row node list, and a progress bar reading
"~1540h 53m left".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

VIEWS = Path(__file__).resolve().parents[1] / "app/ui/static/views"


# ── the ETA was measuring calendar time, not work ────────────────────────

def _rows(n, *, start, busy_s, gap_s, status="done"):
    """n nodes, each busy_s long, separated by gap_s of nobody doing anything —
    an assist job where a human runs a step, then sleeps."""
    out, t = [], start
    for _ in range(n):
        out.append(SimpleNamespace(status=status, title="x", started_at=t,
                                   completed_at=t + timedelta(seconds=busy_s)))
        t += timedelta(seconds=busy_s + gap_s)
    return out


def test_idle_gaps_do_not_become_the_rate():
    """The live shape: 117 timed nodes across ~5 days, 15 left, "~1540h left"."""
    from app.modules.execution_handler import _compute_read_progress as prog
    start = datetime(2026, 9, 25, tzinfo=timezone.utc)
    rows = _rows(20, start=start, busy_s=60, gap_s=6 * 3600) + [
        SimpleNamespace(status="pending", title="y", started_at=None, completed_at=None)
        for _ in range(5)]
    p = prog(rows, "executing")
    # 20 nodes x 60s of real work, 5 left -> a few minutes, not days
    assert p["eta_ms"] is not None
    assert p["eta_ms"] < 60 * 60 * 1000, f"{p['eta_human']} — gaps leaked into the rate"


def test_concurrent_work_still_counts_once():
    """§17.812's property must survive: overlapping nodes are not additive."""
    from app.modules.execution_handler import _compute_read_progress as prog
    t = datetime(2026, 9, 25, tzinfo=timezone.utc)
    rows = [SimpleNamespace(status="done", title="x", started_at=t,
                            completed_at=t + timedelta(seconds=100)) for _ in range(4)]
    rows.append(SimpleNamespace(status="pending", title="y", started_at=None, completed_at=None))
    p = prog(rows, "executing")
    assert p["eta_ms"] == 25_000, p["eta_ms"]      # 100s of wall time / 4 done x 1 left


def test_an_estimate_beyond_a_week_is_not_printed():
    from app.modules.execution_handler import _compute_read_progress as prog
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [SimpleNamespace(status="done", title="x", started_at=t,
                            completed_at=t + timedelta(days=30))]
    rows += [SimpleNamespace(status="pending", title="y", started_at=None, completed_at=None)
             for _ in range(5)]
    p = prog(rows, "executing")
    assert p["eta_ms"] is None and p["eta_human"] is None
    assert "left" not in p["summary"], p["summary"]


# ── one strip, one badge, one account ────────────────────────────────────

def test_the_run_tab_does_not_draw_a_second_stage_strip():
    src = (VIEWS / "theater.js").read_text()
    assert src.count("steps: false") == 2, "both flowGuide calls must ask for the hint only"
    fg = (VIEWS / "flow_guide.js").read_text()
    assert "steps = true" in fg and "steps ? el(" in fg, "the strip must be optional"


def test_the_toolbar_does_not_repeat_the_job_badge():
    src = (VIEWS / "theater.js").read_text()
    blk = src[src.index("function setStatusPill"):src.index("function announceTerminal")]
    assert "nextActionChips(" in blk, "the actions are not duplicated and must stay"
    assert "statusBadge(status)" not in blk, "the hub already renders this badge"


def test_a_decision_lights_its_step_in_the_list():
    src = (VIEWS / "theater.js").read_text()
    for fn in ("function showRunApproval(d) {", "function showDecision(d) {"):
        blk = src[src.index(fn):][:600]
        assert "currentKey =" in blk and "renderNodes()" in blk, fn
        assert "scrollToCurrent()" in blk, f"{fn} must bring it into view"


def test_the_standing_panel_steps_back_when_a_card_owns_now():
    src = (VIEWS / "theater.js").read_text()
    blk = src[src.index("function renderStanding()"):][:900]
    assert 'summaryEl.classList.contains("hidden")' in blk, \
        "the panel must know whether a card is already describing now"
    assert "the step above is the one waiting on you" in blk


def test_every_card_reveal_re_renders_the_panel():
    """renderStanding runs during load, BEFORE a card is revealed — without this
    the panel keeps its full verdict and the two argue."""
    src = (VIEWS / "theater.js").read_text()
    assert src.count('summaryEl.classList.remove("hidden");') == \
        src.count("queueMicrotask(() => { if (!disposed) renderStanding(); });"), \
        "a reveal that does not re-render leaves two accounts on screen"


def test_bringing_a_step_into_view_does_not_scroll_the_page():
    """Caught in a screenshot: `scrollIntoView` walks up and scrolls every
    scrollable ancestor, so lighting a row in the side column took the job
    title and the stage strip off the top of the screen."""
    src = (VIEWS / "theater.js").read_text()
    blk = src[src.index("function scrollToCurrent()"):][:700]
    assert "nodeListEl.scrollTop" in blk, blk
    # the CALL, not the word — the comment above it names the thing it avoids
    assert "row.scrollIntoView(" not in blk and ".scrollIntoView({" not in blk, blk
