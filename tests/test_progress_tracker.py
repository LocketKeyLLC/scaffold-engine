"""Tests for app/utils/progress.py — ProgressTracker / EmitThrottle (§17.811)."""
import pytest

from app.utils.progress import EmitThrottle, ProgressTracker, humanize_ms


class FakeClock:
    """Injectable monotonic clock for deterministic ETA math."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


# ---------------------------------------------------------------------------
# humanize_ms
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.parametrize(
    "ms,expected",
    [
        (None, "unknown"),
        (0, "0s"),
        (5000, "5s"),
        (65000, "1m 05s"),
        (200000, "3m 20s"),
        (3900000, "1h 05m"),
        (-100, "0s"),
    ],
)
def test_humanize_ms(ms, expected):
    assert humanize_ms(ms) == expected


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_cold_start_has_no_eta():
    clk = FakeClock()
    pt = ProgressTracker(4, phase="executing", unit="nodes", clock=clk)
    snap = pt.snapshot()
    assert snap["completed"] == 0
    assert snap["pct"] == 0
    assert snap["eta_ms"] is None
    assert snap["eta_human"] is None
    assert snap["summary"] == "0/4 nodes · 0%"


# ---------------------------------------------------------------------------
# Elapsed-rate ETA (§17.812 — concurrency-correct; replaced the per-unit EWMA)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_eta_from_uniform_rate():
    clk = FakeClock()
    pt = ProgressTracker(4, phase="executing", unit="nodes", alpha=0.5, clock=clk)
    clk.advance(10)
    snap = pt.tick(1, current_item="auth", done_items=["scaffold"])
    # 10s for the first unit → 3 remaining × 10s = 30s
    assert snap["completed"] == 1
    assert snap["pct"] == 25
    assert snap["eta_ms"] == 30000
    assert snap["eta_human"] == "~30s"
    assert snap["current_item"] == "auth"
    assert snap["done_items"] == ["scaffold"]

    clk.advance(10)
    snap = pt.tick(2)
    assert snap["completed"] == 2
    assert snap["eta_ms"] == 20000  # steady 10s/unit × 2 remaining
    assert snap["summary"] == "2/4 nodes · 50% · ~20s left"


@pytest.mark.smoke
def test_eta_tracks_average_elapsed_rate():
    # §17.812 — ETA = elapsed / units_done × remaining. After 2 units in 12s of
    # wall-clock, the average is 6s/unit, so 8 remaining → 48s.
    clk = FakeClock()
    pt = ProgressTracker(10, phase="x", clock=clk)
    clk.advance(2)
    pt.tick(1)
    clk.advance(10)
    snap = pt.tick(2)  # elapsed=12, done=2 → 6s/unit × 8 remaining = 48s
    assert snap["eta_ms"] == 48000


@pytest.mark.smoke
def test_parallel_wave_eta_reflects_wall_clock_not_interarrival():
    # §17.812 (audit M2) — the core fix. A wave of 4 nodes each ~60s wall finishes
    # together at t=60; the drain ticks them back-to-back (inter-arrival ~0). The
    # OLD per-unit EWMA folded those near-zero gaps and collapsed the ETA toward
    # zero. Elapsed-rate: 60s / 4 done = 15s effective per node × 4 remaining = 60s.
    clk = FakeClock()
    pt = ProgressTracker(8, phase="executing", unit="nodes", clock=clk)
    clk.advance(60)
    pt.tick(1)
    pt.tick(2)
    pt.tick(3)
    snap = pt.tick(4)  # all at t=60 (burst)
    assert snap["completed"] == 4
    assert snap["eta_ms"] == 60000  # NOT ~0 (the old EWMA bug)


@pytest.mark.smoke
def test_resume_baseline_excluded_from_rate():
    # §17.812 — a resumed run counts pre-resume completions toward pct but NOT the
    # rate: only work done THIS session (completed - initial) / elapsed.
    clk = FakeClock()
    pt = ProgressTracker(10, phase="x", initial_completed=6, clock=clk)
    clk.advance(10)
    snap = pt.tick(8)  # 2 done this session in 10s → 5s/unit × 2 remaining = 10s
    assert snap["completed"] == 8
    assert snap["eta_ms"] == 10000


@pytest.mark.smoke
def test_default_tick_increments():
    clk = FakeClock()
    pt = ProgressTracker(3, phase="x", clock=clk)
    clk.advance(5)
    snap = pt.tick()  # no arg → +1
    assert snap["completed"] == 1


@pytest.mark.smoke
def test_stale_count_updates_labels_and_eta_reflects_stall():
    # §17.812 — a repeated (non-advancing) tick still updates the labels, and the
    # elapsed-rate ETA GROWS to reflect the stall (no progress for 100s → the
    # estimate widens honestly, rather than freezing as the old EWMA did).
    clk = FakeClock()
    pt = ProgressTracker(4, phase="x", clock=clk)
    clk.advance(10)
    pt.tick(1)
    eta_before = pt.snapshot()["eta_ms"]  # 10s/unit × 3 = 30s
    clk.advance(100)  # long idle, no forward progress
    snap = pt.tick(1, current_item="still-node-1")  # same count
    assert snap["current_item"] == "still-node-1"
    assert snap["eta_ms"] > eta_before  # ETA widens to reflect the stall
    assert snap["completed"] == 1  # count unchanged


# ---------------------------------------------------------------------------
# Soft total
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_soft_total_renders_upper_bound():
    clk = FakeClock()
    pt = ProgressTracker(
        10, phase="researching", unit="iterations", soft_total=True, clock=clk
    )
    clk.advance(20)
    snap = pt.tick(1)
    assert snap["soft"] is True
    assert snap["eta_human"].startswith("≤")


# ---------------------------------------------------------------------------
# Unknown / zero total
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_zero_total_degrades_gracefully():
    clk = FakeClock()
    pt = ProgressTracker(0, phase="x", unit="docs", clock=clk)
    assert pt.snapshot()["pct"] is None
    clk.advance(5)
    snap = pt.tick(1)
    assert snap["eta_ms"] is None
    assert snap["summary"] == "1/? docs"


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_completion_zeroes_eta():
    clk = FakeClock()
    pt = ProgressTracker(2, phase="x", clock=clk)
    clk.advance(5)
    pt.tick(1)
    clk.advance(5)
    snap = pt.tick(2)
    assert snap["pct"] == 100
    assert snap["eta_ms"] == 0


# ---------------------------------------------------------------------------
# EmitThrottle
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_throttle_first_and_interval_and_final():
    clk = FakeClock()
    thr = EmitThrottle(5.0, clock=clk)
    assert thr.ready() is True  # first always fires
    assert thr.ready() is False  # too soon
    clk.advance(5)
    assert thr.ready() is True  # interval elapsed
    clk.advance(1)
    assert thr.ready(final=True) is True  # final always fires
    assert thr.ready() is False  # ...but final didn't reset early


@pytest.mark.smoke
def test_throttle_zero_interval_always_ready():
    clk = FakeClock()
    thr = EmitThrottle(0.0, clock=clk)
    assert thr.ready() is True
    assert thr.ready() is True
