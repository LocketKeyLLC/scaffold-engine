"""§17.812 (audit M2) — _compute_read_progress ETA is concurrency-correct and
agrees with the live SSE ProgressTracker for the same job.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.modules.execution_handler import _compute_read_progress
from app.utils.progress import ProgressTracker

_BASE = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _row(status, title, start=None, end=None):
    return SimpleNamespace(status=status, title=title, started_at=start, completed_at=end)


@pytest.mark.smoke
def test_read_eta_divides_by_concurrency():
    # 4 nodes ran concurrently in one 60s wall-clock window; 4 still pending.
    rows = [_row("done", f"n{i}", _BASE, _BASE + timedelta(seconds=60)) for i in range(4)]
    rows += [_row("pending", f"p{i}") for i in range(4)]
    snap = _compute_read_progress(rows, job_status="running")
    # span 60s / 4 done = 15s effective per node × 4 remaining = 60000ms.
    # OLD (mean-duration × remaining) = 60000 × 4 = 240000 — a 4× over-estimate.
    assert snap["eta_ms"] == 60000
    assert snap["pct"] == 50


@pytest.mark.smoke
def test_read_eta_serial_matches_mean():
    # Serial: two nodes back-to-back, 30s each (0–30, 30–60). span 60s / 2 = 30s.
    rows = [
        _row("done", "a", _BASE, _BASE + timedelta(seconds=30)),
        _row("done", "b", _BASE + timedelta(seconds=30), _BASE + timedelta(seconds=60)),
        _row("running", "c"),
        _row("pending", "d"),
    ]
    snap = _compute_read_progress(rows, job_status="running")
    assert snap["eta_ms"] == 60000  # 30s/node × 2 remaining


@pytest.mark.smoke
def test_read_eta_none_when_terminal():
    rows = [_row("done", f"n{i}", _BASE, _BASE + timedelta(seconds=10)) for i in range(3)]
    assert _compute_read_progress(rows, job_status="completed")["eta_ms"] is None


@pytest.mark.smoke
def test_read_and_sse_eta_agree_under_parallelism():
    """The two surfaces must not diverge by ~concurrency× (the M2 symptom)."""
    # Read-path: 4 done concurrently over 60s, 4 remaining → 60000ms.
    rows = [_row("done", f"n{i}", _BASE, _BASE + timedelta(seconds=60)) for i in range(4)]
    rows += [_row("pending", f"p{i}") for i in range(4)]
    read_eta = _compute_read_progress(rows, job_status="running")["eta_ms"]

    # SSE tracker: same job — 8 total, a 4-wide wave completes at t=60 (tracker
    # constructed at t=0 so elapsed is a real 60s).
    class _Clk:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            return self.t

    clk = _Clk()
    pt = ProgressTracker(8, phase="executing", unit="nodes", clock=clk)
    clk.t = 60.0
    for i in range(1, 5):
        pt.tick(i)
    sse_eta = pt.snapshot()["eta_ms"]

    assert read_eta == sse_eta == 60000
